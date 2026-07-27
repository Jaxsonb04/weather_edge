"""Best-effort ladder-depth capture for the research book.

The scanner and account policy have only ever seen TOP-of-book size
(``yes_bid_size_fp`` / ``yes_ask_size_fp`` from the market listing endpoint).
Whether walking one or two ticks deeper into the book would let the research
sleeve capture more of its approved edge is currently unanswerable -- that
data was never recorded. This module fetches and normalizes the public
``/markets/{ticker}/orderbook`` response (verified live against
api.elections.kalshi.com/trade-api/v2: ``{"orderbook_fp": {"yes_dollars":
[[price_str, size_str], ...], "no_dollars": [...]}}``, dollar-string prices,
best price last in each side's list).

This is observation only. It never influences a gate, a size, or a price, and
a failure here must never affect a scan or an order:
``capture_orderbook_depth`` catches every exception and returns ``None``
rather than propagating.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrderbookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderbookDepth:
    """Top-of-book-and-deeper resting orders, best price last (as returned)."""

    yes: tuple[OrderbookLevel, ...]
    no: tuple[OrderbookLevel, ...]


def parse_orderbook_response(payload: object) -> OrderbookDepth | None:
    """Parse the Kalshi orderbook payload. Returns ``None`` on any malformed shape.

    Pure and total: never raises. An empty book (a real, valid state for an
    illiquid or just-listed market) parses to empty tuples, not ``None``.
    """

    if not isinstance(payload, dict):
        return None
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        return None
    yes = _parse_side(book.get("yes_dollars"))
    no = _parse_side(book.get("no_dollars"))
    if yes is None or no is None:
        return None
    return OrderbookDepth(yes=yes, no=no)


def _parse_side(raw: object) -> tuple[OrderbookLevel, ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None
    levels: list[OrderbookLevel] = []
    for entry in raw:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 2
        ):
            return None
        price, size = entry
        try:
            price_f = float(price)
            size_f = float(size)
        except (TypeError, ValueError):
            return None
        if not (0.0 <= price_f <= 1.0) or size_f < 0.0:
            return None
        levels.append(OrderbookLevel(price=price_f, size=size_f))
    return tuple(levels)


def capture_orderbook_depth(client: Any, ticker: str, *, levels: int = 3) -> OrderbookDepth | None:
    """Fetch and parse one market's orderbook. Never raises; ``None`` on any failure.

    ``client`` is duck-typed to ``KalshiPublicClient`` (accepts anything with a
    matching ``get_orderbook`` method) so tests can pass a lightweight fake
    without constructing a real HTTP client.
    """

    try:
        payload = client.get_orderbook(ticker, depth=levels)
    except Exception:  # noqa: BLE001 -- best-effort telemetry, must never raise
        return None
    return parse_orderbook_response(payload)


def depth_levels_json(levels: tuple[OrderbookLevel, ...]) -> list[list[float]]:
    """Compact JSON-ready form: ``[[price, size], ...]``, preserving order."""

    return [[level.price, level.size] for level in levels]
