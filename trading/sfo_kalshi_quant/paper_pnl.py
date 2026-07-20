"""Canonical pure accounting arithmetic for resolved paper position lots."""

from __future__ import annotations


def settled_position_pnl(
    contracts: float,
    cost_per_contract: float,
    position_wins: bool,
) -> float:
    """Return terminal settlement P&L from fee-inclusive entry cost."""

    return contracts * (
        (1.0 - cost_per_contract) if position_wins else -cost_per_contract
    )


def closed_position_pnl(
    contracts: float,
    cost_per_contract: float,
    exit_price: float,
    exit_fee_per_contract: float,
) -> float:
    """Return close P&L from fee-inclusive entry and exit proceeds."""

    return contracts * (
        exit_price - exit_fee_per_contract - cost_per_contract
    )
