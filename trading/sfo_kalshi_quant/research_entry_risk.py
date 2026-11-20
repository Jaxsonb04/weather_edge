"""Additional entry-only safety bounds for the existing Research ROI account.

These are conservative risk-design choices, not profit-validated tuning. Keep
the fixed account/goal policy intact so tightening risk never resets history.
"""

from __future__ import annotations

import math

from .research_policy import TARGET_POLICY


RESEARCH_ENTRY_RISK_VERSION = "research-entry-risk-v4-market-signal-2026-11-12"
# v4 (2026-11-12, research book v8): entries price off the M1 market
# recalibration, cross the displayed ask only, and spend a flat $90 per leg
# once the after-fee edge clears one cent. v3 (2026-09-13) tagged the
# stop-scaled daily room charge, the dropped behind-bid fallback and the
# 30-minute day-ahead rest; see git history for its measurements.
TARGET_ENTRY_FULL_LOSS_CAP = 90.0
# v8 holds every position to settlement, so an open position does not stop out
# into today's realized loss. The 25% REALIZED pause in
# target_remaining_daily_risk is the hard daily bound; open positions no longer
# consume that room (owner decision 2026-11-12: "losing is fine").
TARGET_OPEN_RISK_CHARGE_FRACTION = 0.0

# Resting maker TTL (2026-09-13). Since 2026-11-12 the v8 target book is
# taker-only, so the 30 minutes below now reaches only replayed history; the
# stale-quote guard it once needed was removed.
# Every book rested 15 minutes. Measured on the research target sleeve: maker
# fill delays run to 15.4 min and 8 of 43 maker fills landed in the LAST 3 minutes of the window, i.e. the fill
# hazard is not exhausted at expiry; and the fresh public tape shows
# day-ahead seller flow BUILDING through the US evening (22Z-07Z hours each
# carry 1.5-4x the 14Z open hour, which is only 8.9% of day-ahead volume),
# so an open-hour-only extension would rest on a refuted premise. The
# target sleeve's day-ahead quotes therefore rest 30 minutes at every hour.
# Live keeps 15.
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
# Reason prefix the stale-quote guard stamps on the orders it pulls. The
# research goal report splits these from genuine TTL expiries, because the
# TTL-only share is the seller-flow evidence (execution.py) and a guard pull
# is not an unfilled rest.
STALE_RESEARCH_QUOTE_REASON_PREFIX = "stale research quote"


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
    """Maximum entry cost at risk, including fees: flat, not Kelly.

    The v8 book spends up to the $90 per-position cap on any leg whose
    lower bound covers the fee-inclusive cost. Its decisions carry the M1
    recalibrated probability minus the one-cent entry edge as that bound, so
    the condition is exactly p - ask - taker fee >= 0.01. Malformed inputs,
    or no edge, fund nothing; displayed depth and the account caps still clip.
    """

    cost_value = _finite_number(cost)
    probability = _finite_number(probability_lcb)
    if (
        cost_value is None
        or probability is None
        or not 0.0 < cost_value < 1.0
        or not 0.0 <= probability <= 1.0
        or probability - cost_value < -1e-12
    ):
        return 0.0
    return min(
        TARGET_ENTRY_FULL_LOSS_CAP,
        TARGET_POLICY.reference_equity * TARGET_POLICY.max_position_risk_pct,
    )


def target_remaining_daily_risk(realized_pnl: object, active_risk: object) -> float:
    """Reserve stop-scaled open/pending loss against the daily loss budget.

    Profits do not increase the budget. Open cost and resting reservations
    are charged at TARGET_OPEN_RISK_CHARGE_FRACTION of cost (zero for the
    hold-to-settlement v8 book). The hard bound is the realized pause: this
    returns 0 once today's booked loss reaches the policy's daily-loss budget.
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
