"""Canonical city-scoped settlement truth for research and accounting.

The same calendar date can settle fifteen different markets.  Every lookup is
therefore keyed by ``(series_ticker, target_date)``.  Date-only inputs remain a
temporary SFO compatibility path for old callers and fixtures; they can never
settle another city's row.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import TypeAlias

from .cities import city_for_market_ticker, city_for_station

SettlementKey: TypeAlias = tuple[str, str]


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
    """Load authoritative CLI outcomes, dropping unknown/retired stations."""

    rows = conn.execute(
        "SELECT station_id, local_date, max_temperature_f FROM cli_settlements "
        "WHERE max_temperature_f IS NOT NULL"
    ).fetchall()
    truth: dict[SettlementKey, float] = {}
    for station_id, local_date, high in rows:
        try:
            city = city_for_station(str(station_id))
        except KeyError:
            continue
        truth[(city.series_ticker, str(local_date))] = float(high)
    return truth
