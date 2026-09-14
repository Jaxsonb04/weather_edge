"""Additional entry-only safety bounds for the existing Research ROI account.

These are conservative risk-design choices, not profit-validated tuning. Keep
the fixed account/goal policy intact so tightening risk never resets history.
"""

from __future__ import annotations

import math

from .research_policy import TARGET_POLICY


RESEARCH_ENTRY_RISK_VERSION = "research-entry-risk-v3-scaling-2026-09-13"
# Preserve the existing production ceiling. Strong conservative edges may use
# more than the undeployed $30 containment design allowed, but nothing here can
# exceed what production already permitted -- see target_entry_spend_limit.
# The v3 bump (2026-09-13) loosens only the DAILY room charge below
# (TARGET_OPEN_RISK_CHARGE_FRACTION); the per-entry limit is unchanged.
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
