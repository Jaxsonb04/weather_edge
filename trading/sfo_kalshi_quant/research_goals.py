"""Immutable daily objective state for the isolated target-research account."""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from .exit_audit import audited_exit_reason
from .logical_positions import LogicalPaperPosition


_PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class DailyGoalState:
    """One Pacific civil day's realized target-account objective state."""

    objective_day: date
    realized_pnl: float
    target_pnl: float
    remaining_pnl: float
    achieved: bool
    locked: bool


def daily_goal_state(
    *,
    objective_day: date,
    realized_pnl: float,
    target_pnl: float,
) -> DailyGoalState:
    """Build state from a frozen goal and realized, after-fee lot P&L."""

    realized = float(realized_pnl)
    target = float(target_pnl)
    if not math.isfinite(realized):
        raise ValueError("daily realized P&L must be finite")
    if not math.isfinite(target) or target <= 0:
        raise ValueError("daily target P&L must be finite and positive")
    achieved = realized + 1e-9 >= target
    return DailyGoalState(
        objective_day=objective_day,
        realized_pnl=realized,
        target_pnl=target,
        remaining_pnl=max(0.0, target - realized),
        achieved=achieved,
        locked=achieved,
    )


def summarize_daily_goals(
    states: Iterable[DailyGoalState],
    *,
    positions: Iterable[LogicalPaperPosition] = (),
    target_feasible: bool | None = None,
    available_conservative_expected_profit: float | None = None,
    feasibility_evidence: str = "unavailable",
    reference_equity: float = 1000.0,
    policy_version: str = "research-target-v1",
) -> dict[str, object]:
    """Return an honest target-only history with explicit zero days."""

    ordered = sorted(states, key=lambda state: state.objective_day)
    hits = sum(state.achieved for state in ordered)
    zero_pnl_days = sum(abs(state.realized_pnl) <= 1e-12 for state in ordered)
    observed = len(ordered)
    reference = float(reference_equity)
    if not math.isfinite(reference) or reference <= 0:
        raise ValueError("research reference equity must be finite and positive")
    daily_pnls = [state.realized_pnl for state in ordered]
    mean_daily = statistics.fmean(daily_pnls) if daily_pnls else None
    median_daily = statistics.median(daily_pnls) if daily_pnls else None
    p25 = _quantile(daily_pnls, 0.25)
    p75 = _quantile(daily_pnls, 0.75)
    stddev = statistics.pstdev(daily_pnls) if daily_pnls else None
    bootstrap_ci = _day_cluster_bootstrap_interval(daily_pnls)
    max_drawdown_dollars, max_drawdown_pct, log_growth = (
        _growth_and_drawdown(daily_pnls, reference_equity=reference)
    )
    valid_positions = [position for position in positions if position.valid]
    terminal_positions = [
        position for position in valid_positions if position.terminal
    ]
    resolved_lots = [
        lot for position in valid_positions for lot in position.resolved_lots
    ]
    activity_days = {
        resolved_day
        for lot in resolved_lots
        if (
            resolved_day := _pacific_day(
                lot.get("closed_at") or lot.get("settled_at")
            )
        )
        is not None
    }
    activity_days.update(
        state.objective_day
        for state in ordered
        if abs(state.realized_pnl) > 1e-12
    )
    resolution_days = {
        resolved_day
        for lot in resolved_lots
        if (
            resolved_day := _pacific_day(
                lot.get("closed_at") or lot.get("settled_at")
            )
        )
        is not None
    }
    independent_city_targets = {
        (
            str(position.root.get("market_ticker") or "").split("-", 1)[0],
            str(position.root.get("target_date") or ""),
        )
        for position in terminal_positions
        if position.root.get("market_ticker") and position.root.get("target_date")
    }
    lead_split = _lead_split(terminal_positions)
    execution = _execution_metrics(valid_positions)
    exit_breakdown = _exit_breakdown(valid_positions)
    current = ordered[-1] if ordered else None
    return {
        "account_id": "paper-research-target-v1",
        "sleeve": "target",
        "policy_version": policy_version,
        "timezone": "America/Los_Angeles",
        "metric": "daily_realized_pnl_from_fixed_reference_equity",
        "objective_day": current.objective_day.isoformat() if current else None,
        "realized_pnl": current.realized_pnl if current else 0.0,
        "target_pnl": current.target_pnl if current else None,
        "remaining_pnl": current.remaining_pnl if current else None,
        "achieved": current.achieved if current else False,
        "locked": current.locked if current else False,
        "status": "hit" if current and current.achieved else "miss",
        "observed_days": observed,
        "zero_activity_days": sum(
            state.objective_day not in activity_days for state in ordered
        ),
        "zero_pnl_days": zero_pnl_days,
        "hit_count": hits,
        "attainment_rate": hits / observed if observed else None,
        "mean_daily_pnl": mean_daily,
        "median_daily_pnl": median_daily,
        "p25_daily_pnl": p25,
        "p75_daily_pnl": p75,
        "daily_pnl_stddev": stddev,
        "day_cluster_bootstrap_95_ci": bootstrap_ci,
        "maximum_drawdown_dollars": max_drawdown_dollars,
        "maximum_drawdown_pct": max_drawdown_pct,
        "log_growth": log_growth,
        "log_growth_per_day": (
            log_growth / observed if log_growth is not None and observed else None
        ),
        "logical_decisions": len(terminal_positions),
        "resolved_lots": len(resolved_lots),
        "resolution_days": len(resolution_days),
        "independent_city_target_days": len(independent_city_targets),
        "lead_split": lead_split,
        "execution": execution,
        "exit_breakdown": exit_breakdown,
        "target_feasible": target_feasible,
        "feasibility_evidence": feasibility_evidence,
        "available_conservative_expected_profit": (
            available_conservative_expected_profit
        ),
        "days": [
            {
                "objective_day": state.objective_day.isoformat(),
                "realized_pnl": state.realized_pnl,
                "target_pnl": state.target_pnl,
                "remaining_pnl": state.remaining_pnl,
                "achieved": state.achieved,
                "locked": state.locked,
            }
            for state in ordered
        ],
        "disclaimer": (
            "Hard paper-research objective; not a guaranteed return. "
            "Risk and edge gates remain binding."
        ),
    }


def _pacific_day(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_PACIFIC).date()


def _quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _day_cluster_bootstrap_interval(
    daily_pnls: list[float],
    *,
    samples: int = 4000,
) -> dict[str, object]:
    """Deterministically resample whole civil days, never individual trades."""

    if not daily_pnls:
        return {
            "method": "deterministic_day_cluster_bootstrap",
            "samples": samples,
            "lower": None,
            "upper": None,
        }
    rng = random.Random(20260717)
    size = len(daily_pnls)
    means = [
        statistics.fmean(rng.choice(daily_pnls) for _ in range(size))
        for _ in range(samples)
    ]
    return {
        "method": "deterministic_day_cluster_bootstrap",
        "samples": samples,
        "lower": _quantile(means, 0.025),
        "upper": _quantile(means, 0.975),
    }


def _growth_and_drawdown(
    daily_pnls: Iterable[float],
    *,
    reference_equity: float,
) -> tuple[float, float, float | None]:
    equity = reference_equity
    peak = reference_equity
    max_drawdown_dollars = 0.0
    max_drawdown_pct = 0.0
    log_growth = 0.0
    valid_growth = True
    for daily_pnl in daily_pnls:
        prior = equity
        equity += daily_pnl
        if prior <= 0 or equity <= 0:
            valid_growth = False
        elif valid_growth:
            log_growth += math.log(equity / prior)
        peak = max(peak, equity)
        drawdown_dollars = peak - equity
        drawdown_pct = drawdown_dollars / peak if peak > 0 else 0.0
        max_drawdown_dollars = max(max_drawdown_dollars, drawdown_dollars)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
    return (
        max_drawdown_dollars,
        max_drawdown_pct,
        log_growth if valid_growth else None,
    )


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _lead_split(
    positions: Iterable[LogicalPaperPosition],
) -> dict[str, dict[str, float | int]]:
    split: dict[str, dict[str, float | int]] = {}
    for position in positions:
        row = position.as_row()
        lead = str(row.get("lead_bucket") or "unknown")
        bucket = split.setdefault(
            lead,
            {
                "logical_decisions": 0,
                "resolved_lots": 0,
                "realized_pnl": 0.0,
                "capital_resolved": 0.0,
            },
        )
        bucket["logical_decisions"] = int(bucket["logical_decisions"]) + 1
        bucket["resolved_lots"] = int(bucket["resolved_lots"]) + len(
            position.resolved_lots
        )
        bucket["realized_pnl"] = float(bucket["realized_pnl"]) + float(
            row.get("realized_pnl") or 0.0
        )
        bucket["capital_resolved"] = float(bucket["capital_resolved"]) + float(
            row.get("capital_resolved") or 0.0
        )
    for bucket in split.values():
        capital = float(bucket["capital_resolved"])
        bucket["roi"] = (
            float(bucket["realized_pnl"]) / capital if capital > 0 else None
        )
    return split


def _execution_metrics(
    positions: Iterable[LogicalPaperPosition],
) -> dict[str, object]:
    rows = list(positions)
    roots = [position.root for position in rows]
    requested = 0.0
    filled = 0.0
    partial_fill_orders = 0
    slippage: list[float] = []
    for root in roots:
        requested_contracts = _finite(root.get("requested_contracts"))
        filled_contracts = _finite(root.get("filled_contracts"))
        if requested_contracts is None:
            requested_contracts = _finite(root.get("contracts")) or 0.0
        if filled_contracts is None:
            filled_contracts = (
                requested_contracts
                if root.get("status")
                not in {"PAPER_LIMIT_RESTING", "PAPER_EXPIRED"}
                else 0.0
            )
        requested += max(0.0, requested_contracts)
        filled += max(0.0, min(filled_contracts, requested_contracts))
        if 1e-9 < filled_contracts < requested_contracts - 1e-9:
            partial_fill_orders += 1
        entry_price = _finite(root.get("entry_price"))
        entry_ask = _finite(root.get("entry_ask"))
        if entry_price is not None and entry_ask is not None:
            slippage.append(entry_price - entry_ask)

    lots = [lot for position in rows for lot in position.resolved_lots]
    entry_fees = math.fsum(
        (_finite(lot.get("fee_per_contract")) or 0.0)
        * (_finite(lot.get("contracts")) or 0.0)
        for lot in lots
    )
    exit_fees = math.fsum(
        (_finite(lot.get("exit_fee_per_contract")) or 0.0)
        * (_finite(lot.get("contracts")) or 0.0)
        for lot in lots
    )
    return {
        "orders": len(roots),
        "maker_orders": sum(root.get("entry_mode") == "limit" for root in roots),
        "taker_orders": sum(root.get("entry_mode") != "limit" for root in roots),
        "requested_contracts": requested,
        "filled_contracts": filled,
        "fill_rate": filled / requested if requested > 0 else None,
        "partial_fill_orders": partial_fill_orders,
        "partial_exit_positions": sum(
            len(position.resolved_lots) > 1 for position in rows
        ),
        "expired_orders": sum(
            root.get("status") in {"PAPER_EXPIRED", "PAPER_PARTIAL_EXPIRED"}
            for root in roots
        ),
        "entry_fees": entry_fees,
        "exit_fees": exit_fees,
        "total_fees": entry_fees + exit_fees,
        "mean_entry_slippage_per_contract": (
            statistics.fmean(slippage) if slippage else None
        ),
    }


def _exit_breakdown(
    positions: Iterable[LogicalPaperPosition],
) -> dict[str, dict[str, float | int]]:
    breakdown: dict[str, dict[str, float | int]] = {}
    for position in positions:
        lots = list(position.resolved_lots)
        root = position.root
        if not lots and audited_exit_reason(root) == "expired_unfilled":
            lots = [root]
        reasons_for_position: set[str] = set()
        for lot in lots:
            reason = audited_exit_reason(lot)
            if reason == "unclassified":
                continue
            bucket = breakdown.setdefault(
                reason,
                {
                    "logical_decisions": 0,
                    "resolved_lots": 0,
                    "realized_pnl": 0.0,
                },
            )
            bucket["resolved_lots"] = int(bucket["resolved_lots"]) + int(
                reason != "expired_unfilled"
            )
            bucket["realized_pnl"] = float(bucket["realized_pnl"]) + float(
                lot.get("realized_pnl") or 0.0
            )
            reasons_for_position.add(reason)
        for reason in reasons_for_position:
            breakdown[reason]["logical_decisions"] = (
                int(breakdown[reason]["logical_decisions"]) + 1
            )
    return breakdown
