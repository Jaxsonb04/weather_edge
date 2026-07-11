"""Canonical city-scoped settlement truth for research and accounting.

The same calendar date can settle fifteen different markets.  Every lookup is
therefore keyed by ``(series_ticker, target_date)``.  Date-only inputs remain a
temporary SFO compatibility path for old callers and fixtures; they can never
settle another city's row.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import TypeAlias

from ._util import _optional_float, _parse_timestamp, _row_value
from .cities import city_for_market_ticker, city_for_station

SettlementKey: TypeAlias = tuple[str, str]


def integer_settlement_high_f(value: object) -> float:
    """Round a raw daily high to the integer used for market settlement."""

    high = float(value)
    if not math.isfinite(high):
        raise ValueError("settlement high must be finite")
    return float(math.floor(high + 0.5))


def bin_resolves_yes(
    strike_type: str | None,
    floor_strike: object,
    cap_strike: object,
    settlement_high_f: float,
) -> bool:
    """Canonical typed-bin rule used by ``MarketBin.resolves_yes`` and rows."""

    strike = str(strike_type or "")
    floor_value = _optional_float(floor_strike)
    cap_value = _optional_float(cap_strike)
    if strike == "less":
        return cap_value is not None and settlement_high_f < cap_value
    if strike == "greater":
        return floor_value is not None and settlement_high_f > floor_value
    # MarketBin has always treated every other typed strike as a bounded bin.
    return (
        floor_value is not None
        and cap_value is not None
        and floor_value <= settlement_high_f <= cap_value
    )


def label_resolves_yes(label: str, settlement_high_f: float) -> bool:
    """Compatibility parser for legacy rows that predate typed strike fields."""

    if "or below" in label:
        match = re.search(r"(\d+)", label)
        return bool(match and settlement_high_f <= float(match.group(1)))
    if "or above" in label:
        match = re.search(r"(\d+)", label)
        return bool(match and settlement_high_f >= float(match.group(1)))
    match = re.search(r"(\d+).+?(\d+)", label)
    if match:
        lo, hi = float(match.group(1)), float(match.group(2))
        return lo <= settlement_high_f <= hi
    return False


def row_resolves_yes(row: object, settlement_high_f: float) -> bool:
    """Resolve a SQLite/dict-shaped bin through the canonical typed rule."""

    strike_type = _row_value(row, "strike_type")
    floor_strike = _optional_float(_row_value(row, "floor_strike"))
    cap_strike = _optional_float(_row_value(row, "cap_strike"))
    if strike_type or floor_strike is not None or cap_strike is not None:
        return bin_resolves_yes(
            str(strike_type) if strike_type is not None else None,
            floor_strike,
            cap_strike,
            settlement_high_f,
        )
    return label_resolves_yes(str(_row_value(row, "label", "") or ""), settlement_high_f)


def is_pre_resolution_decision(row: object) -> bool:
    """Whether a recorded decision can be proven to predate resolution."""

    if _row_value(row, "intraday_is_complete", 0, default_on_none=True):
        return False
    created_at = _parse_timestamp(_row_value(row, "created_at"))
    close_time = _parse_timestamp(_row_value(row, "market_close_time"))
    if created_at is None:
        return True
    if close_time is None:
        return False
    return created_at < close_time


def normalize_settlement_truth(
    settlements: Mapping[object, float],
) -> dict[SettlementKey, float]:
    normalized: dict[SettlementKey, float] = {}
    for raw_key, raw_high in settlements.items():
        if isinstance(raw_key, tuple) and len(raw_key) == 2:
            series, target = raw_key
        else:
            # Legacy WeatherEdge research was SFO-only.  Preserve that narrow
            # contract without allowing a date-only value to leak to any city.
            series, target = "KXHIGHTSFO", raw_key
        target_iso = target.date().isoformat() if isinstance(target, datetime) else (
            target.isoformat() if isinstance(target, date) else str(target)
        )
        normalized[(str(series).strip().upper(), target_iso)] = float(raw_high)
    return normalized


def settlement_key_for_market(ticker: str, target_date: object) -> SettlementKey | None:
    city = city_for_market_ticker(ticker)
    if city is None:
        return None
    target_iso = target_date.isoformat() if isinstance(target_date, date) else str(target_date)
    return city.series_ticker, target_iso


def settlement_for_market(
    settlements: Mapping[SettlementKey, float],
    ticker: str,
    target_date: object,
) -> float | None:
    key = settlement_key_for_market(ticker, target_date)
    return settlements.get(key) if key is not None else None


def load_cli_settlement_truth(conn) -> dict[SettlementKey, float]:
    """Load final CLI outcomes; legacy schemas without finality fail closed."""

    columns = {row[1] for row in conn.execute("PRAGMA table_info(cli_settlements)")}
    if "is_final" not in columns:
        return {}
    rows = conn.execute(
        "SELECT station_id, local_date, max_temperature_f FROM cli_settlements "
        "WHERE max_temperature_f IS NOT NULL AND is_final = 1"
    ).fetchall()
    truth: dict[SettlementKey, float] = {}
    for station_id, local_date, high in rows:
        try:
            city = city_for_station(str(station_id))
        except KeyError:
            continue
        truth[(city.series_ticker, str(local_date))] = float(high)
    return truth
