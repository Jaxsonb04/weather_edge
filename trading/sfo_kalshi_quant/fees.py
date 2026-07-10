from __future__ import annotations

import math

FEE_SCHEDULE_VERSION = "2026-07-07"
FEE_ROUNDING_UNIT = 0.0001  # one centicent

# July 7, 2026 non-standard table. Unlisted prediction series use maker M=0,
# taker M=1. Only overrides that differ from that general rule are needed here.
_MAKER_ONE = {
    "KXAAAGASM", "KXATPMATCH", "KXBALLONDOR", "KXBTCMAX150", "KXCPI",
    "KXCPIYOY", "KXEGGS", "KXEMMYCACTO", "KXEMMYCACTR", "KXEMMYCSERIES",
    "KXEMMYDACTO", "KXEMMYDACTR", "KXEMMYDSERIES", "KXFED",
    "KXFEDDECISION", "KXGDP", "KXHEISMAN", "KXINXY", "KXIPO", "KXLALIGA",
    "KXLLM1", "KXMARMAD", "KXMENWORLDCUP", "KXMLB", "KXMLBAL",
    "KXMLBASGAME", "KXMLBGAME", "KXMLBNL", "KXNASDAQ100Y", "KXNBA",
    "KXNBAEAST", "KXNBAMVP", "KXNBAROY", "KXNBAWEST", "KXNCAAF",
    "KXNCAAFACC", "KXNCAAFB10", "KXNCAAFB12", "KXNCAAFGAME",
    "KXNCAAFPLAYOFF", "KXNCAAFSEC", "KXNFLAFCCHAMP", "KXNFLAFCEAST",
    "KXNFLAFCNORTH", "KXNFLAFCSOUTH", "KXNFLAFCWEST", "KXNFLCOTY",
    "KXNFLCPOTY", "KXNFLDPOTY", "KXNFLDROTY", "KXNFLGAME", "KXNFLMVP",
    "KXNFLNFCCHAMP", "KXNFLNFCEAST", "KXNFLNFCNORTH", "KXNFLNFCSOUTH",
    "KXNFLNFCWEST", "KXNFLOPOTY", "KXNFLOROTY", "KXNHL", "KXNHLEAST",
    "KXNHLWEST", "KXPAYROLLS", "KXPGARYDER", "KXPGASOLHEIM", "KXPGATOUR",
    "KXRATECUTCOUNT", "KXSB", "KXSUPERBOWLHEADLINE", "KXU3", "KXUCL",
    "KXUCLGAME", "KXWCGAME", "KXWNBA", "KXWNBAGAME", "KXWTAMATCH",
}
_TAKER_ZERO = {
    "KXBTCY", "KXCITRINI", "KXDOED", "KXELECTIRAN", "KXETHY",
    "KXGAMBLINGREPEAL", "KXGREENLAND", "KXLAYOFFSYINFO", "KXPAHLAVIHEAD",
}


def fee_multipliers(series_or_ticker: str | None) -> tuple[float, float]:
    series = (series_or_ticker or "").split("-", 1)[0].upper()
    return (1.0 if series in _MAKER_ONE else 0.0, 0.0 if series in _TAKER_ZERO else 1.0)


def _ceil_position_plus_fee(position_cost: float, raw_fee: float) -> float:
    total = math.ceil((position_cost + raw_fee) / FEE_ROUNDING_UNIT - 1e-12) * FEE_ROUNDING_UNIT
    return max(0.0, round(total - position_cost, 12))


def ceil_to_cent(value: float) -> float:
    """Round fees up to the next cent.

    Kalshi's fee schedule rounds exchange fees to cents. For strategy testing we
    intentionally round conservatively so edge is not overstated.
    """

    if value <= 0:
        return 0.0
    return math.ceil((value * 100.0) - 1e-12) / 100.0


def quadratic_fee_total(
    price: float,
    contracts: float = 1.0,
    *,
    maker: bool = False,
    fee_multiplier: float = 1.0,
    taker_rate: float = 0.07,
    maker_rate: float = 0.0175,
    series_ticker: str | None = None,
) -> float:
    """Estimate total Kalshi fee for binary weather contracts.

    Prices are represented in dollars from 0.00 to 1.00. The quadratic fee is
    largest near 50c and shrinks near 0c/100c.
    """

    if price <= 0 or price >= 1 or contracts <= 0:
        return 0.0
    rate = maker_rate if maker else taker_rate
    schedule_multiplier = 1.0
    if series_ticker is not None:
        maker_multiplier, taker_multiplier = fee_multipliers(series_ticker)
        schedule_multiplier = maker_multiplier if maker else taker_multiplier
    raw_fee = rate * fee_multiplier * schedule_multiplier * contracts * price * (1.0 - price)
    if series_ticker is not None:
        return _ceil_position_plus_fee(price * contracts, raw_fee)
    return ceil_to_cent(raw_fee)


def quadratic_fee_per_contract(
    price: float,
    *,
    maker: bool = False,
    fee_multiplier: float = 1.0,
    taker_rate: float = 0.07,
    maker_rate: float = 0.0175,
    series_ticker: str | None = None,
) -> float:
    return quadratic_fee_total(
        price,
        1.0,
        maker=maker,
        fee_multiplier=fee_multiplier,
        taker_rate=taker_rate,
        maker_rate=maker_rate,
        series_ticker=series_ticker,
    )


def quadratic_fee_average_per_contract(
    price: float,
    contracts: float,
    *,
    maker: bool = False,
    fee_multiplier: float = 1.0,
    taker_rate: float = 0.07,
    maker_rate: float = 0.0175,
    series_ticker: str | None = None,
) -> float:
    if contracts <= 0:
        return 0.0
    return quadratic_fee_total(
        price,
        contracts,
        maker=maker,
        fee_multiplier=fee_multiplier,
        taker_rate=taker_rate,
        maker_rate=maker_rate,
        series_ticker=series_ticker,
    ) / contracts


def contracts_for_budget(price: float, budget: float) -> float:
    if price <= 0 or price >= 1 or budget <= 0:
        return 0.0
    lo = 0.0
    hi = budget / price
    for _ in range(48):
        mid = (lo + hi) / 2.0
        cost = price * mid + quadratic_fee_total(price, mid)
        if cost <= budget:
            lo = mid
        else:
            hi = mid
    return lo


def expected_profit_per_yes_contract(win_probability: float, ask_price: float, fee: float) -> float:
    """Expected dollar profit for buying one YES contract at ask."""

    return win_probability * (1.0 - ask_price) - (1.0 - win_probability) * ask_price - fee


def kelly_fraction_spent(win_probability: float, cost: float) -> float:
    """Kelly fraction of bankroll to spend on a binary contract.

    A YES contract costs ``cost`` and pays 1.00 if it resolves YES. The returned
    fraction is the Kelly fraction of bankroll allocated to the purchase cost.
    """

    if cost <= 0 or cost >= 1:
        return 0.0
    edge = win_probability - cost
    if edge <= 0:
        return 0.0
    return edge / (1.0 - cost)
