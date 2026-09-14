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

Capture is best effort: a failure here must never affect a scan or an order.
``capture_orderbook_depth`` catches every exception and returns ``None``
rather than propagating, and ``capture_orderbook_depth_within`` adds a hard
wall-clock deadline for the one caller that sits in front of placement. Since
2026-09-13 the live book's taker cross sizes against a fresh ladder read
through ``side_ask_ladder`` (execution._taker_cross_quote); a missing ladder
there is the historical single-level cross, never a block.
"""

from __future__ import annotations

import math
import threading
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


def capture_orderbook_depth_within(
    client: Any,
    ticker: str,
    *,
    levels: int = 3,
    deadline_seconds: float,
) -> tuple[OrderbookDepth | None, bool]:
    """``capture_orderbook_depth`` under a hard wall-clock deadline. Never raises.

    Returns ``(depth, timed_out)``. The fetch runs on a daemon worker thread
    and the caller waits at most ``deadline_seconds`` for it, whatever the
    socket, a retry loop or a Retry-After header would otherwise do. A fetch
    still in flight at the deadline is abandoned -- its late result is
    discarded and a daemon thread never holds the process open -- and is
    reported as ``(None, True)``. Pair it with a client that does not retry
    and whose socket timeout is no longer than the deadline
    (``KalshiPublicClient.single_attempt``) so an abandoned worker also ends
    promptly. A non-positive or non-finite deadline fetches nothing and
    reports a timeout; a worker that cannot start reports ``(None, False)``.
    """

    try:
        wait = float(deadline_seconds)
    except (TypeError, ValueError, OverflowError):
        return None, True
    if not math.isfinite(wait) or wait <= 0.0:
        return None, True
    outcome: list[OrderbookDepth | None] = []

    def _fetch() -> None:
        outcome.append(capture_orderbook_depth(client, ticker, levels=levels))

    worker = threading.Thread(
        target=_fetch, name=f"orderbook-ladder:{ticker}", daemon=True
    )
    try:
        worker.start()
    except RuntimeError:
        # No thread available (resource limits, interpreter shutdown). A
        # synchronous fetch would defeat the deadline, so fetch nothing.
        return None, False
    worker.join(wait)
    if worker.is_alive():
        return None, True
    return (outcome[0] if outcome else None), False


def depth_levels_json(levels: tuple[OrderbookLevel, ...]) -> list[list[float]]:
    """Compact JSON-ready form: ``[[price, size], ...]``, preserving order."""

    return [[level.price, level.size] for level in levels]


def side_ask_ladder(
    depth: OrderbookDepth,
    side: str,
    *,
    levels: int = 2,
) -> tuple[tuple[float, float], ...]:
    """Resting offers a buyer of ``side`` would lift, best (lowest) price first.

    Kalshi publishes only bids: ``yes_dollars`` are YES bids and
    ``no_dollars`` are NO bids (docs.kalshi.com/getting_started/
    orderbook_responses). A YES buyer lifts NO bids at ``1 - no_bid``; a NO
    buyer lifts YES bids at ``1 - yes_bid``. Verified against the public API
    on 2026-09-13: the KXHIGHNY-26SEP13-B80.5 listing showed yes_ask 0.19 x
    34 while the best ``no_dollars`` entry of its book was 0.81 x 34.

    Pure and total. Empty or zero-size levels are dropped; the result is
    sorted by price so it never depends on the API's list order, and is
    truncated to ``levels`` entries (the two-level cross hard-caps at two).
    """

    bids = depth.no if str(side).upper() == "YES" else depth.yes
    offers: list[tuple[float, float]] = []
    for level in bids:
        price = round(1.0 - float(level.price) + 1e-12, 6)
        size = float(level.size)
        if size <= 0.0 or not 0.0 < price < 1.0:
            continue
        offers.append((price, size))
    offers.sort(key=lambda entry: entry[0])
    return tuple(offers[: max(0, int(levels))])
