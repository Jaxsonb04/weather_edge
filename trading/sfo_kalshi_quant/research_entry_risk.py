"""Additional entry-only safety bounds for the existing Research ROI account.

These are conservative risk-design choices, not profit-validated tuning. Keep
the fixed account/goal policy intact so tightening risk never resets history.
"""

from __future__ import annotations

import math

from .research_policy import TARGET_POLICY


RESEARCH_ENTRY_RISK_VERSION = "research-entry-risk-v3-scaling-2026-09-13"
# Preserve the existing production ceiling while allowing strong conservative
# edges to scale. The temporary $30 containment ceiling was not deployed.
TARGET_ENTRY_FULL_LOSS_CAP = 90.0
TARGET_ENTRY_FRACTIONAL_KELLY = 0.25
# Fraction of every open cost basis and resting reservation charged against
# the remaining daily loss budget. An open position cannot lose its whole
# cost today: the 35% stop is the designed loss, and the worst observed stop
# overshoot is the three 2026-09-11/12 incident exits, which realized 61.05%,
# 40.76% and 53.51% of cost. Charging the worst of those (0.60) instead of
# 100% keeps the daily budget a bound on what today's stops can actually
# realize. At the 100% charge a replay of 145 two-hourly published snapshots
# (2026-08-31..09-12; open+pending mean $178, median $148, p90 $414) had
# zero room in 51.7% of snapshots; at 0.60 it is 22.8%. The best research
# days (9/3 +$10.59, 9/4 +$15.13, 9/10 +$16.93) all ran $198-$417 open.
TARGET_OPEN_RISK_CHARGE_FRACTION = 0.60


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
    """Reserve stop-scaled open/pending loss against the daily loss budget.

    Profits do not increase the budget. Charge every open target date because
    any position can stop out today; no settlement offsets fund new entries.
    Filled cost and resting reservations are charged alike, at
    TARGET_OPEN_RISK_CHARGE_FRACTION of cost: a stop realizes a bounded
    slice of cost, never all of it, and the fraction is the worst observed
    overshoot. The $150 realized pause, the $90 per-entry cap and the
    fractional-Kelly entry limit are unchanged.
    """

    realized = _finite_number(realized_pnl)
    active = _finite_number(active_risk)
    if realized is None or active is None or active < 0.0:
        return 0.0
    return max(
        0.0,
        TARGET_POLICY.reference_equity * TARGET_POLICY.daily_loss_pause_pct
        + min(realized, 0.0)
        - TARGET_OPEN_RISK_CHARGE_FRACTION * active,
    )
