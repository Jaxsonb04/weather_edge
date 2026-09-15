"""Additional entry-only safety bounds for the existing Research ROI account.

These are conservative risk-design choices, not profit-validated tuning. Keep
the fixed account/goal policy intact so tightening risk never resets history.
"""

from __future__ import annotations

import math

from .research_policy import TARGET_POLICY


RESEARCH_ENTRY_RISK_VERSION = "research-entry-risk-v3-scaling-2026-09-13"
# Preserve the existing production ceiling. Strong conservative edges may use
# more than the undeployed $30 containment design allowed, but no entry can
# exceed what production already permitted -- see target_entry_spend_limit.
# The v3 bump (2026-09-13) tags the research execution changes of the scaling
# release: the stop-scaled DAILY room charge (TARGET_OPEN_RISK_CHARGE_FRACTION),
# the dropped behind-bid reservation fallback, and the 30-minute day-ahead
# resting TTL below. The per-entry limit is unchanged.
TARGET_ENTRY_FULL_LOSS_CAP = 90.0
TARGET_ENTRY_FRACTIONAL_KELLY = 0.25
# Fraction of every open cost basis and resting reservation charged against
# the remaining daily loss budget. An open position is not expected to lose
# its whole cost today (the 35% stop is the designed loss), but 0.60 is a
# DESIGN fraction, not a worst case. Public research ledger
# (strategy_research.json generated 2026-09-13T01:20Z, paper-research-roi-v6,
# 22 losing closes): per-position loss/cost has median 0.61 and max 0.734
# (KXHIGHMIA-26SEP06-B92.5, $1.88 cost); 12 of 22 exceed 0.60, all but one
# of them on positions under $7 of cost. Cost-weighted realization is 0.36
# over all losers and 0.34 over the six losers of $10 or more. The three
# 2026-09-11/12 incident exits (61.05%, 40.76%, 53.51% of cost) are the
# largest DOLLAR losses, not the worst ratios. So the projected room is a
# planning estimate, not a bound: $250 open charged at 0.60 fills the $150
# budget while a 73% day on that book would realize $183. The hard bound is
# the $150 REALIZED pause (target_remaining_daily_risk returns 0 once
# realized <= -$150), which halts new entries after the loss is booked; the
# charge only decides how early entries stop before that.
# PAPER CALIBRATION: every ratio above is a paper exit (the monitor closes
# the remainder at the displayed bid on its 2-minute cadence, with
# displayed-depth partial exits). A real-money stop on a thin NO book
# realizes more (the Atlanta 9/11 exit already needed 6 fills at $74 of
# cost), and the previous 100% charge was the one budget input independent
# of exit quality. Re-derive this fraction from real fills before any
# real-money use.
# WHY NOT 100%: scripts/replay_research_daily_room.py replays the published
# gh-pages snapshots at two-hour boundaries (2026-08-31..09-12, 154 samples;
# open+pending mean $170, median $148, p90 $388): zero room in 52.6% of
# samples at the 100% charge, 20.1% at 0.60, 0.6% at 0.35. (The track spec
# quoted a 145-sample replay at 51.7% / 22.8% / 5.5%, mean $178, p90 $414;
# same picture, different sampling.) The best research days (9/3 +$10.59,
# 9/4 +$15.13, 9/10 +$16.93; verified from the same ledger) peaked at
# $217-$417 open+pending, all forbidden at the 100% charge.
# COMPOSED WITH THE 30-MINUTE REST (release review 2026-09-13): that replay
# sampled snapshots taken under the 15-minute research rest. Resting
# reservations are charged into the same projected room (the research capacity
# read charges every PAPER_LIMIT_RESTING reserved_cost), and 72.5% of research
# orders expired unfilled, so the 30-minute day-ahead rest roughly doubles the
# pending term. Re-scoring the same 154 samples with only that term scaled
# (replay_research_daily_room.py --pending-scale), at this 0.60 charge: zero
# room in 20.1% of samples at x1.0, 23.4% at x1.5, 27.9% at x2.0; mean room
# $57.63, $55.61, $54.86. An approximation -- some extra rest converts to
# fills, moving cost from pending to open rather than removing it. The two
# levers together cost frequency, not safety: the $150 realized pause stays
# the hard bound. Re-derive this fraction from the first post-deploy week. If
# the owner wants the full frequency of both levers, charge resting
# reservations by their measured fill share rather than by this
# filled-position fraction.
TARGET_OPEN_RISK_CHARGE_FRACTION = 0.60

# Resting maker TTL (2026-09-13). Every book rested 15 minutes. Measured on
# the research target sleeve: maker fill delays run to 15.4 min and 8 of 43
# maker fills landed in the LAST 3 minutes of the window, i.e. the fill
# hazard is not exhausted at expiry; and the fresh public tape shows
# day-ahead seller flow BUILDING through the US evening (22Z-07Z hours each
# carry 1.5-4x the 14Z open hour, which is only 8.9% of day-ahead volume),
# so an open-hour-only extension would rest on a refuted premise. The
# target sleeve's day-ahead quotes therefore rest 30 minutes at every hour.
# The longer rest doubles stale-quote exposure, which is why the scan now
# pulls a resting quote whose CURRENT after-fee LCB edge has gone negative
# (paper.PaperTrader.cancel_stale_research_resting_orders): its expiry is cut
# to the scan instant, so tape that traded through it BEFORE that instant is
# still credited, and the monitor cancels the remainder once the public tape
# covers the instant plus the ingestion grace. Live keeps 15.
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
    """Maximum entry cost at risk, including fees, under conservative Kelly.

    Quarter-Kelly (``TARGET_ENTRY_FRACTIONAL_KELLY``) on the probability's
    lower confidence bound against the fee-inclusive cost, capped by
    ``TARGET_ENTRY_FULL_LOSS_CAP`` ($90) and the policy's per-position risk
    limit. Malformed inputs, or no conservative edge, fund nothing.

    Read this as CONTAINMENT, not scaling (owner PR #121 decision (a),
    re-checked 2026-09-13). No entry can exceed the $90 per-position ceiling
    production already had: every term sits under a ``min`` with it. That
    does NOT make it shrink-only. Thin conservative edges are cut hard, but a
    strong LCB edge can still be sized UP to $90, including structural taker
    candidates that the pre-#121 25-contract taker truncation held smaller:
    this limit is the quantity target of
    ``paper._expanded_structural_target_taker`` and
    ``research_portfolio._target_sized_decision``, not only a cap. The
    2026-09-12 strategy-performance note's code-path regression ($0.76 ask,
    100 contracts displayed, 0.90 LCB) grows a quote from 25 contracts
    ($19.32 with fees) to 100 ($77.28). The 2026-09-13 re-check estimated
    roughly a 40% cut in entered cost overall; that figure was not
    re-measured here, and whether it nets out the taker growth above needs
    the production database. The three 2026-09-12 incident entries alone
    show a 46% cut ($193.85 entered vs $104.98 capped). There is NO measured
    P&L gain yet: no settled paper cohort shows the resized entries improve
    research P&L. What it buys today is bounded exposure on thin conservative
    edges. Do not cite it as a profit-validated sizing improvement, and do
    not raise the fraction or the cap on its strength, until that evidence
    exists.
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
    """Reserve stop-scaled open/pending loss against the daily loss budget.

    Profits do not increase the budget. Charge every open target date because
    any position can stop out today; no settlement offsets fund new entries.
    Filled cost and resting reservations are charged alike, at
    TARGET_OPEN_RISK_CHARGE_FRACTION of cost. That fraction is a design
    estimate of what a stop realizes (per-position paper ratios have run
    to 0.73), so the room is a planning figure; the hard bound is the $150
    realized pause below, which returns 0 once today's booked loss reaches
    the budget. The $90 per-entry cap and the fractional-Kelly entry limit
    are unchanged.
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
