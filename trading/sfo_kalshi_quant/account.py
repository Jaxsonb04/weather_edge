"""Shared paper-account identity, strategy fingerprints, and risk constants."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import time
from zoneinfo import ZoneInfo
from dataclasses import asdict
from typing import Iterable, Sequence

from .config import StrategyConfig, normalize_risk_profile_name
from .research_policy import MOTION_POLICY, TARGET_POLICY, ResearchSleeve

SHARED_ACCOUNT_ID = "paper-shared"
INITIAL_CAPITAL = 1000.0

# Audit AC-01: research experiments are paper-only forever and must not be
# able to erase live gains or trigger live risk pauses. New research orders
# book against this separate VIRTUAL account (its own ledger, cash, pauses,
# and drawdown) so the production-intent live equity and weekly return target
# are measured on the live book alone. Historical research rows that already
# consumed shared capital keep their original account: the shared history
# (including the legacy -$87.30) is never rewritten.
RESEARCH_ACCOUNT_ID = "paper-research-shadow"
RESEARCH_VIRTUAL_CAPITAL = INITIAL_CAPITAL
ACCOUNTING_POLICY_VERSION = "acct-v4-account-scoped-2026-07-14"
WEEKLY_RETURN_TARGET = 0.05
WEEKLY_GOAL_TZ = ZoneInfo("America/Los_Angeles")
WEEKLY_GOAL_ROLLOVER = time(0, 0)


def research_shared_capital_enabled() -> bool:
    """Explicit opt-in for a shared-capital research experiment (off by default).

    Some research questions genuinely need queue/capital competition with the
    live book; enabling this env flag restores the old co-mingled behavior for
    NEW research orders. It must be a deliberate, visually distinct choice.
    """

    raw = os.getenv("PAPER_RESEARCH_SHARED_CAPITAL_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def account_for_profile(risk_profile: str | None) -> str:
    profile = normalize_risk_profile_name(risk_profile) if risk_profile else "live"
    if profile == "research" and not research_shared_capital_enabled():
        return RESEARCH_ACCOUNT_ID
    return SHARED_ACCOUNT_ID


def account_for_research_sleeve(sleeve: ResearchSleeve) -> str:
    """Route a new research sleeve to its isolated virtual account."""

    if sleeve is ResearchSleeve.TARGET:
        return TARGET_POLICY.account_id
    if sleeve is ResearchSleeve.MOTION:
        return MOTION_POLICY.account_id
    raise ValueError(f"unsupported research sleeve: {sleeve!r}")


MIN_EXECUTABLE_NOTIONAL = 5.0
# Per-position ceiling: min(NORMAL_POSITION_CAP, NORMAL_POSITION_PCT * equity).
# Raised 2026-07-10 from $20/2% to $30/3%: with maker-first sizing no longer
# bound by displayed ask depth, the per-position cap becomes the working
# per-trade ceiling (~$30 on the $1000 bankroll). Aggregate, sleeve, city,
# region, daily-loss, and drawdown breakers are unchanged.
NORMAL_POSITION_CAP = 30.0
AGGREGATE_RISK_PCT = 0.20
MAIN_SLEEVE_PCT = 0.16
RESEARCH_SLEEVE_PCT = 0.04
RESEARCH_POSITION_PCT = 0.01
NORMAL_POSITION_PCT = 0.03
CITY_TARGET_PCT = 0.05
REGION_DAY_PCT = 0.08
DAILY_LOSS_PCT = 0.02

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
    daily_pnl: float,
    target_date: str,
    market_ticker: str,
    risk_profile: str | None,
    requested_spend: float,
) -> dict[str, object]:
    """Apply shared-account risk policy to already-loaded account state.

    Database reads deliberately stay in ``PaperStore``; this function is pure
    policy math so caps and pause behavior can be tested without SQLite.
    """

    equity = float(state["realized_equity"])
    drawdown = float(state["drawdown"])
    if drawdown >= 0.15:
        return {"allowed_spend": 0.0, "reason": "15% account drawdown pause"}
    if daily_pnl <= -DAILY_LOSS_PCT * equity:
        return {"allowed_spend": 0.0, "reason": "2% shared-account daily loss pause"}

    rows = list(active_rows)
    series = market_ticker.split("-", 1)[0].upper()
    region = REGION_BY_SERIES.get(series, "unknown")
    profile = normalize_risk_profile_name(risk_profile) if risk_profile else "live"
    aggregate = sum(float(row[3] or 0.0) for row in rows)
    research_risk = sum(float(row[3] or 0.0) for row in rows if str(row[2]) == "research")
    main_risk = aggregate - research_risk
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
        sleeve_room = MAIN_SLEEVE_PCT * equity - main_risk + max(
            0.0, RESEARCH_SLEEVE_PCT * equity - research_risk
        )
    allowed = min(
        requested_spend,
        position_cap,
        total_room,
        sleeve_room,
        CITY_TARGET_PCT * equity - city_risk,
        REGION_DAY_PCT * equity - region_risk,
        float(state["available_cash"]),
    )
    if requested_spend < MIN_EXECUTABLE_NOTIONAL:
        return {"allowed_spend": 0.0, "reason": "recommendation below $5 executable minimum"}
    if allowed < MIN_EXECUTABLE_NOTIONAL:
        return {"allowed_spend": 0.0, "reason": "account risk room below $5 executable minimum"}
    return {"allowed_spend": max(0.0, allowed), "reason": None}


def strategy_fingerprint(config: StrategyConfig | None, *, entry_mode: str) -> str:
    if config is None:
        return "legacy_independent_sizing"
    payload = {
        "strategy": asdict(config),
        "execution": {"entry_mode": entry_mode, "account_policy": "shared-v2"},
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
