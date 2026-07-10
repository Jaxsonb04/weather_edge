"""Shared paper-account identity, strategy fingerprints, and risk constants."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .config import StrategyConfig

SHARED_ACCOUNT_ID = "paper-shared"
INITIAL_CAPITAL = 1000.0
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
