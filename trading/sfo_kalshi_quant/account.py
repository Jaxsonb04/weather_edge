"""Paper-account identities, strategy fingerprints, and risk constants."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import time
from zoneinfo import ZoneInfo
from dataclasses import asdict
from typing import Iterable, Sequence

from .config import StrategyConfig, normalize_risk_profile_name
from .research_policy import MOTION_POLICY, TARGET_POLICY, ResearchSleeve

SHARED_ACCOUNT_ID = "paper-shared"
LIVE_STABILITY_ACCOUNT_ID = "paper-live-stability-v1"
INITIAL_CAPITAL = 1000.0

# Audit AC-01: historical generic research remains isolated in this archived
# virtual ledger. New research admission requires an explicit active policy
# account; the former shared-capital environment escape hatch is retired.
RESEARCH_ACCOUNT_ID = "paper-research-shadow"
RESEARCH_VIRTUAL_CAPITAL = INITIAL_CAPITAL
ACCOUNTING_POLICY_VERSION = "acct-v4-account-scoped-2026-07-14"
# Explicitly covers behavior outside StrategyConfig (notably exit policy). Any
# live-account behavior change must rotate this value so readiness evidence
# cannot silently blend pre-change and post-change orders.
STRATEGY_BEHAVIOR_VERSION = "behavior-v3-forecast-and-execution-2026-09-07"
WEEKLY_RETURN_TARGET = 0.05
WEEKLY_GOAL_TZ = ZoneInfo("America/Los_Angeles")
WEEKLY_GOAL_ROLLOVER = time(0, 0)


def account_for_profile(risk_profile: str | None) -> str:
    profile = normalize_risk_profile_name(risk_profile) if risk_profile else "live"
    if profile == "research":
        return RESEARCH_ACCOUNT_ID
    return LIVE_STABILITY_ACCOUNT_ID


def account_for_research_sleeve(sleeve: ResearchSleeve) -> str:
    """Return a sleeve's canonical account; admission status is checked later."""

    if sleeve is ResearchSleeve.TARGET:
        return TARGET_POLICY.account_id
    if sleeve is ResearchSleeve.MOTION:
        return MOTION_POLICY.account_id
    raise ValueError(f"unsupported research sleeve: {sleeve!r}")


MIN_EXECUTABLE_NOTIONAL = 5.0
# Per-position ceiling: min(NORMAL_POSITION_CAP, NORMAL_POSITION_PCT * equity).
# Raised 2026-07-10 from $20/2% to $30/3%: with maker-first sizing no longer
# bound by displayed ask depth, the per-position cap becomes the working
# per-trade ceiling (~$30 on the $1000 bankroll). Aggregate, city, region and
# drawdown breakers are unchanged.
NORMAL_POSITION_CAP = 30.0
AGGREGATE_RISK_PCT = 0.20
# Audit TC-6 (2026-09-07): MAIN_SLEEVE_PCT = 0.16 was DELETED, not retuned. It
# split the aggregate cap into a "main" and a "research" sleeve, but since the
# v3 account cutover every research order lives in its own policy account and
# `active_rows` binds only paper-shared plus the entry account, so a live
# entry's rows structurally cannot contain a research row. With
# research_risk == 0 the old expression
#     0.16E - main_risk + max(0, 0.04E - research_risk)
# reduces algebraically to 0.20E - aggregate -- exactly AGGREGATE_RISK_PCT,
# which now stands alone. (It is also never looser than the old expression:
# the two differ only when research_risk > 0.04E, where the old one gave MORE
# room.) Research capital is capped by its own sleeve policy in
# `PaperStore._research_capacity_on_connection`, not here.
RESEARCH_SLEEVE_PCT = 0.04
RESEARCH_POSITION_PCT = 0.01
NORMAL_POSITION_PCT = 0.03
CITY_TARGET_PCT = 0.05
REGION_DAY_PCT = 0.08
# Audit TC-6 (2026-09-07): DAILY_LOSS_PCT = 0.02 ("2% live-account daily loss
# pause") was DELETED because in this system's configuration it can never
# fire. Both paper entry paths (`PaperTrader.place_approved` and
# `PaperTrader.place_arbitrage`) consult `PaperStore.paper_entry_pause_reason`
# BEFORE any capacity call, over realized pnl on the same fixed-PST settlement
# day, and PAUSE_THRESHOLDS["live"] pauses at 1.0% of the caller's bankroll.
#
# Be precise about how strong that claim is: the two breakers are NOT
# identically scoped, so this is domination in the production configuration,
# not by construction.
#   * Row set: the deleted query took status IN (PAPER_SETTLED, PAPER_CLOSED)
#     filtered by account_id and ignored risk_profile; the surviving one takes
#     any non-NULL realized_pnl row that is not REJECTED/PAPER_EXPIRED,
#     filtered by risk_profile, and -- since both entry paths call it with no
#     account_id -- ignores account. Broader row set, so it can only see at
#     least as much loss.
#   * Threshold: 1.0% of the passed-in `bankroll`, not 2% of realized equity.
#     Callers pass `_clamp_sizing_equity(equity, 1000)` = clamp(equity, 500,
#     2000), so the surviving pause fires at $5.00 below $500 of equity, at
#     1% of equity between $500 and $2000, and at $20.00 above -- strictly
#     tighter than 2% of equity at every level. A caller passing a far larger
#     --bankroll would invert that; nothing in production does, and
#     `test_account_capacity_carries_no_unreachable_daily_loss_breaker` keeps
#     the deletion a visible decision rather than a silent gap.
#
# Daily loss is enforced by that breaker for paper entries, and by
# `LiveExecutionPolicy.daily_loss_pct` (SFO_LIVE_DAILY_LOSS_PCT, still 2% of
# risk capital) for real money.

REGION_BY_SERIES = {
    "KXHIGHMIA": "southeast",
    "KXHIGHLAX": "west-coast",
    "KXHIGHCHI": "midwest",
    "KXHIGHTATL": "southeast",
    "KXHIGHNY": "northeast",
    "KXHIGHTDAL": "texas",
    "KXHIGHTSEA": "west-coast",
    "KXHIGHPHIL": "northeast",
    "KXHIGHTPHX": "southwest",
    "KXHIGHAUS": "texas",
    "KXHIGHTSFO": "west-coast",
    "KXHIGHTHOU": "texas",
    "KXHIGHTOKC": "southern-plains",
    "KXHIGHTBOS": "northeast",
    "KXHIGHDEN": "mountain",
}


def policy_capacity(
    *,
    state: dict[str, object],
    active_rows: Iterable[Sequence[object]],
    target_date: str,
    market_ticker: str,
    risk_profile: str | None,
    requested_spend: float,
    minimum_notional: float = MIN_EXECUTABLE_NOTIONAL,
) -> dict[str, object]:
    """Apply shared-account risk policy to already-loaded account state.

    Database reads deliberately stay in ``PaperStore``; this function is pure
    policy math so caps and pause behavior can be tested without SQLite.
    """

    equity = float(state["realized_equity"])
    drawdown = float(state["drawdown"])
    if drawdown >= 0.15:
        return {"allowed_spend": 0.0, "reason": "15% account drawdown pause"}

    rows = list(active_rows)
    series = market_ticker.split("-", 1)[0].upper()
    region = REGION_BY_SERIES.get(series, "unknown")
    profile = normalize_risk_profile_name(risk_profile) if risk_profile else "live"
    aggregate = sum(float(row[3] or 0.0) for row in rows)
    research_risk = sum(float(row[3] or 0.0) for row in rows if str(row[2]) == "research")
    city_risk = sum(
        float(row[3] or 0.0)
        for row in rows
        if str(row[0]).startswith(series + "-") and str(row[1]) == target_date
    )
    region_risk = sum(
        float(row[3] or 0.0)
        for row in rows
        if REGION_BY_SERIES.get(str(row[0]).split("-", 1)[0].upper(), "unknown") == region
        and str(row[1]) == target_date
    )
    position_cap = (
        RESEARCH_POSITION_PCT * equity
        if profile == "research"
        else min(NORMAL_POSITION_CAP, NORMAL_POSITION_PCT * equity)
    )
    if drawdown >= 0.10:
        position_cap *= 0.5
    total_room = AGGREGATE_RISK_PCT * equity - aggregate
    if profile == "research":
        sleeve_room = RESEARCH_SLEEVE_PCT * equity - research_risk
    else:
        # No main/research split survives the v3 cutover -- see the
        # MAIN_SLEEVE_PCT note above. The aggregate cap IS the live sleeve.
        sleeve_room = total_room
    allowed = min(
        requested_spend,
        position_cap,
        total_room,
        sleeve_room,
        CITY_TARGET_PCT * equity - city_risk,
        REGION_DAY_PCT * equity - region_risk,
        float(state["available_cash"]),
    )
    try:
        minimum_notional = float(minimum_notional)
    except (TypeError, ValueError):
        return {"allowed_spend": 0.0, "reason": "minimum executable notional is invalid"}
    if not math.isfinite(minimum_notional) or minimum_notional <= 0:
        return {"allowed_spend": 0.0, "reason": "minimum executable notional is invalid"}
    if requested_spend + 1e-9 < minimum_notional:
        return {
            "allowed_spend": 0.0,
            "reason": (
                "recommendation below "
                f"${minimum_notional:g} executable minimum"
            ),
        }
    if allowed + 1e-9 < minimum_notional:
        return {
            "allowed_spend": 0.0,
            "reason": (
                "account risk room below "
                f"${minimum_notional:g} executable minimum"
            ),
        }
    return {"allowed_spend": max(0.0, allowed), "reason": None}


def strategy_fingerprint(config: StrategyConfig | None, *, entry_mode: str) -> str:
    if config is None:
        return "legacy_independent_sizing"
    payload = {
        "strategy": asdict(config),
        "execution": {
            "entry_mode": entry_mode,
            "account_policy": "shared-v2",
            "behavior_version": STRATEGY_BEHAVIOR_VERSION,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def sleeve_for(profile: str | None, reasons: list[str], side: str) -> str:
    if (profile or "").lower() == "research":
        return "research"
    for reason in reasons:
        marker = "sleeve="
        if marker in reason:
            return reason.split(marker, 1)[1].split(",", 1)[0].split(" ", 1)[0].strip()
    return "yes_convex" if side.upper() == "YES" else "no_core"
