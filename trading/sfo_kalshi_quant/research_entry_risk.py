"""Additional entry-only safety bounds for the existing Research ROI account.

These are conservative risk-design choices, not profit-validated tuning. Keep
the fixed account/goal policy intact so tightening risk never resets history.
"""

from __future__ import annotations

import math

from .research_policy import TARGET_POLICY


RESEARCH_ENTRY_RISK_VERSION = "research-entry-risk-v2-scaled-2026-09-12"
# Preserve the existing production ceiling. Strong conservative edges may use
# more than the undeployed $30 containment design allowed, but nothing here can
# exceed what production already permitted -- see target_entry_spend_limit.
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
    """Maximum entry cost at risk, including fees, under conservative Kelly.

    Quarter-Kelly (``TARGET_ENTRY_FRACTIONAL_KELLY``) on the probability's
    lower confidence bound against the fee-inclusive cost, capped by
    ``TARGET_ENTRY_FULL_LOSS_CAP`` ($90) and the policy's per-position risk
    limit. Malformed inputs, or no conservative edge, fund nothing.

    Read this as CONTAINMENT, not scaling (owner PR #121 decision (a),
    re-checked 2026-09-13). Every term sits under a ``min`` with the $90
    per-position ceiling production already had, so this can only shrink an
    entry, never grow one; before it, the allocator sized structural research
    candidates up to that ceiling. The re-check put the effect at roughly a
    40% cut in entered cost (the three 2026-09-12 incident entries, $193.85
    entered in total, would have been capped at $104.98), with NO measured
    P&L gain yet: no settled paper cohort shows the smaller entries improve
    research P&L. What it buys today is bounded exposure on thin conservative
    edges. Do not cite it as a profit-validated sizing improvement, and do not
    raise the fraction or the cap on its strength, until that evidence exists.
    """

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
