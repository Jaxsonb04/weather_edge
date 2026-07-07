from __future__ import annotations

from .models import MarketBin


def fallback_bins(event_ticker: str, center_high_f: float) -> list[MarketBin]:
    """Probability-only paper ladder matching the Kalshi daily-high shape.

    Kalshi lists six markets per event -- an open-bottom tail, four 2°F
    interior bins, and an open-top tail -- re-centered daily around the
    forecast. This builds the same shape around ``center_high_f`` so a target
    with no live event still gets a research ladder in every city's climate,
    instead of the old hardcoded San Francisco strikes.
    """

    base = int(round(center_high_f / 2.0) * 2)  # nearest even integer
    low_cap = base - 4
    payloads: list[dict] = [
        {
            "ticker": f"{event_ticker}-T{low_cap}",
            "event_ticker": event_ticker,
            "title": f"Paper high temperature <{low_cap}",
            "yes_sub_title": f"{low_cap - 1}° or below",
            "strike_type": "less",
            "cap_strike": low_cap,
        }
    ]
    for floor in (base - 4, base - 2, base, base + 2):
        payloads.append(
            {
                "ticker": f"{event_ticker}-B{floor + 0.5}",
                "event_ticker": event_ticker,
                "title": f"Paper high temperature {floor}-{floor + 1}",
                "yes_sub_title": f"{floor}° to {floor + 1}°",
                "strike_type": "between",
                "floor_strike": floor,
                "cap_strike": floor + 1,
            }
        )
    high_floor = base + 3
    payloads.append(
        {
            "ticker": f"{event_ticker}-T{high_floor}",
            "event_ticker": event_ticker,
            "title": f"Paper high temperature >{high_floor}",
            "yes_sub_title": f"{high_floor + 1}° or above",
            "strike_type": "greater",
            "floor_strike": high_floor,
        }
    )
    markets = []
    for payload in payloads:
        payload = {
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "1.0000",
            "no_bid_dollars": "0.0000",
            "no_ask_dollars": "1.0000",
            "yes_bid_size_fp": "0",
            "yes_ask_size_fp": "0",
            "status": "paper",
            **payload,
        }
        markets.append(MarketBin.from_kalshi(payload))
    return markets


def standard_sfo_bins(event_ticker: str = "KXHIGHTSFO-PAPER") -> list[MarketBin]:
    """The historical SFO fallback ladder (center 70F), kept for callers and
    tests that predate the forecast-centered ``fallback_bins``."""

    return fallback_bins(event_ticker, 69.5)
