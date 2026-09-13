"""Additional entry-only safety bounds for the existing Research ROI account.

These are conservative risk-design choices, not profit-validated tuning. Keep
the fixed account/goal policy intact so tightening risk never resets history.
"""

from __future__ import annotations

import math

from .research_policy import TARGET_POLICY


RESEARCH_ENTRY_RISK_VERSION = "research-entry-risk-v2-scaled-2026-09-12"
# Preserve the existing production ceiling while allowing strong conservative
# edges to scale. The temporary $30 containment ceiling was not deployed.
TARGET_ENTRY_FULL_LOSS_CAP = 90.0
TARGET_ENTRY_FRACTIONAL_KELLY = 0.25


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def target_entry_spend_limit(cost: object, probability_lcb: object) -> float:
    """Maximum entry cost at risk, including fees, under conservative Kelly."""

    cost_value = _finite_number(cost)
    probability = _finite_number(probability_lcb)
    if (
        cost_value is None
        or probability is None
        or not 0.0 < cost_value < 1.0
        or not 0.0 <= probability <= 1.0
    ):
        return 0.0
    conservative_fraction = max(0.0, probability - cost_value) / (1.0 - cost_value)
    return min(
        TARGET_ENTRY_FULL_LOSS_CAP,
        TARGET_POLICY.reference_equity * TARGET_POLICY.max_position_risk_pct,
        TARGET_POLICY.reference_equity
        * TARGET_ENTRY_FRACTIONAL_KELLY
        * conservative_fraction,
    )


def target_remaining_daily_risk(realized_pnl: object, active_risk: object) -> float:
    """Reserve full open/pending loss against the remaining daily loss budget.

    Profits do not increase the budget. Charge every open target date because
    any position can stop out today; no settlement offsets fund new entries.
    """

    realized = _finite_number(realized_pnl)
    active = _finite_number(active_risk)
    if realized is None or active is None or active < 0.0:
        return 0.0
    return max(
        0.0,
        TARGET_POLICY.reference_equity * TARGET_POLICY.daily_loss_pause_pct
        + min(realized, 0.0)
        - active,
    )
