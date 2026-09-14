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

# Resting maker TTL (2026-09-13). Every book rested 15 minutes. Measured on
# the research target sleeve: maker fill delays run to 15.4 min and 8 of 43
# maker fills landed in the LAST 3 minutes of the window, i.e. the fill
# hazard is not exhausted at expiry; and the fresh public tape shows
# day-ahead seller flow BUILDING through the US evening (22Z-07Z hours each
# carry 1.5-4x the 14Z open hour, which is only 8.9% of day-ahead volume),
# so an open-hour-only extension would rest on a refuted premise. The
# target sleeve's day-ahead quotes therefore rest 30 minutes at every hour.
# The longer rest doubles stale-quote exposure, which is why the scan now
# cancels a resting quote whose CURRENT after-fee LCB edge has gone negative
# (paper.PaperTrader.cancel_stale_research_resting_orders). Live keeps 15.
#
# OWNER DECISION (2026-09-13): this reverses the 2026-09-12
# strategy-performance note, which kept the 15-minute expiry so forecast
# staleness would not be stretched to manufacture fills. The reversal was
# raised as a sign-off item and the owner APPROVED the 30-minute research
# day-ahead rest to raise research trading frequency.
#
# REAL-MONEY DIVERGENCE: the paper maker model fills a resting quote whenever
# the public tape trades through its price, with no adverse-selection
# haircut. With real money, the fills that arrive late in a longer rest are
# disproportionately adverse -- the counterparty trades into a quote the
# forecast or the book has already moved away from. The extra late-window
# fills this rest harvests therefore overstate real-money value: read the
# paper uplift as an UPPER BOUND on real-money uplift, never as an estimate
# of it. The stale-quote guard mitigates only partly (5-minute scan cadence,
# markets present in that tick only, fresh LCB vs resting after-fee cost
# only), and the 8-of-43 late-fill figure above is itself paper evidence.
DEFAULT_RESTING_ORDER_TTL_MINUTES = 15
TARGET_DAY_AHEAD_RESTING_ORDER_TTL_MINUTES = 30


def resting_order_ttl_minutes(*, account_id: object, lead_bucket: object) -> int:
    """Minutes a resting maker quote rests before TTL expiry.

    Keyword-only because both journal sites call this BEFORE the order row
    exists. Only the research target sleeve's day-ahead quotes get the
    longer rest; the live book, the motion sleeve, and any same-day quote
    keep the historical 15 minutes.
    """

    if account_id == TARGET_POLICY.account_id and lead_bucket == "day-ahead":
        return TARGET_DAY_AHEAD_RESTING_ORDER_TTL_MINUTES
    return DEFAULT_RESTING_ORDER_TTL_MINUTES


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
