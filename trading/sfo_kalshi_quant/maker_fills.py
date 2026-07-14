"""Shared maker-fill normalization and volume allocation.

Single source of execution truth for the live paper monitor and the
chronological replay (audit finding EX-01). Official direction semantics
(docs.kalshi.com/getting_started/order_direction): a public trade has exactly
one aggressor; ``taker_book_side == "bid"`` means the taker bought YES and so
fills resting NO bids, ``taker_book_side == "ask"`` means the taker bought NO
and so fills resting YES bids. Each trade's quantity is finite and is consumed
once, in price-time priority, after each order's estimated queue ahead.

The allocator is a pure, deterministic function of (trades, orders). Cross-pass
conservation is NOT the allocator's job: every capital-consuming fill persists
per-trade volume claims (``maker_volume_claims``), and callers subtract those
claims via ``apply_volume_claims`` before allocating, so consumed volume can
never be credited again by a later pass or a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

MakerSide = Literal["YES", "NO"]

# Versioned execution semantics (audit Batch D): bump when fill/exit semantics
# change so restatements can separate incompatible evidence generations.
# v1: per-order full-history sums, taker_book_side=="bid" for both sides.
# v2: normalized single-aggressor trades, pooled price-time allocation,
#     depth-aware partial exits.
# v3: persist queue depletion as consumed public volume and retain partial
#     maker fills across monitor passes and restarts.
EXECUTION_MODEL_VERSION = "exec-v3-2026-07-14"
EXIT_DEPTH_MAX_AGE_SECONDS = 120.0

_MAKER_SIDE_BY_TAKER_BOOK_SIDE: dict[str, MakerSide] = {"bid": "NO", "ask": "YES"}
_MAKER_SIDE_BY_TAKER_OUTCOME: dict[str, MakerSide] = {"yes": "NO", "no": "YES"}


def depth_observation_is_contemporaneous(
    observed_at: object,
    executed_at: object,
    *,
    max_age_seconds: float = EXIT_DEPTH_MAX_AGE_SECONDS,
) -> bool:
    """True when displayed depth is fresh enough to support an execution."""

    try:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        executed = datetime.fromisoformat(str(executed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if executed.tzinfo is None:
        executed = executed.replace(tzinfo=UTC)
    age_seconds = (executed.astimezone(UTC) - observed.astimezone(UTC)).total_seconds()
    return -5.0 <= age_seconds <= max_age_seconds

_QUANT = Decimal("0.000001")


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(_QUANT)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class PublicAggressorTrade:
    """One public trade normalized to exactly one maker side."""

    trade_id: str
    created_at: datetime
    maker_side: MakerSide
    yes_price: Decimal
    quantity: Decimal

    def side_price(self, side: str) -> Decimal:
        return self.yes_price if side.upper() == "YES" else Decimal(1) - self.yes_price


@dataclass(frozen=True)
class RestingMakerOrder:
    """A resting paper bid as the allocator sees it."""

    order_id: int
    side: MakerSide
    limit_price: Decimal  # in the order's own side price terms
    quantity: Decimal
    queue_ahead: Decimal
    placed_at: datetime


@dataclass(frozen=True)
class AllocatedFill:
    order_id: int
    trade_id: str
    quantity: Decimal
    price: Decimal  # the trade's price in the order's side terms
    queue_consumed: Decimal


@dataclass(frozen=True)
class OrderAllocation:
    order: RestingMakerOrder
    fills: tuple[AllocatedFill, ...]

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), Decimal(0))

    @property
    def queue_consumed(self) -> Decimal:
        return sum((fill.queue_consumed for fill in self.fills), Decimal(0))

    @property
    def complete(self) -> bool:
        return self.filled_quantity >= self.order.quantity

    def allocations_by_trade(self) -> dict[str, float]:
        allocations: dict[str, float] = {}
        for fill in self.fills:
            if fill.quantity > 0:
                allocations[fill.trade_id] = (
                    allocations.get(fill.trade_id, 0.0) + float(fill.quantity)
                )
        return allocations

    def consumption_by_trade(self) -> dict[str, dict[str, float]]:
        """Return every finite unit consumed from each public trade.

        Queue-ahead depletion and contracts filled both spend the same public
        tape. Persisting only the filled contracts lets a later monitor pass
        reuse the queue volume after this order leaves the resting set.
        """

        consumption: dict[str, dict[str, float]] = {}
        for fill in self.fills:
            item = consumption.setdefault(
                fill.trade_id,
                {
                    "queue_quantity": 0.0,
                    "fill_quantity": 0.0,
                    "total_quantity": 0.0,
                },
            )
            item["queue_quantity"] += float(fill.queue_consumed)
            item["fill_quantity"] += float(fill.quantity)
            item["total_quantity"] += float(fill.queue_consumed + fill.quantity)
        return consumption


def normalize_public_trade(payload: dict[str, object]) -> PublicAggressorTrade | None:
    """Normalize one public trade payload to a single-aggressor event.

    Returns ``None`` when the payload cannot deterministically prove the
    aggressor direction, price, quantity, or time -- an unprovable trade must
    never create a fill (audit stop condition: do not invent fills).
    """

    if not isinstance(payload, dict) or payload.get("is_block_trade") is True:
        return None
    trade_id = str(payload.get("trade_id") or "")
    created_at = _parse_time(payload.get("created_time"))
    maker_side = _MAKER_SIDE_BY_TAKER_BOOK_SIDE.get(
        str(payload.get("taker_book_side") or "").lower()
    )
    if maker_side is None:
        maker_side = _MAKER_SIDE_BY_TAKER_OUTCOME.get(
            str(
                payload.get("taker_outcome_side")
                or payload.get("taker_side")
                or ""
            ).lower()
        )
    yes_price = _decimal(payload.get("yes_price_dollars"))
    if yes_price is None:
        yes_price = _decimal(payload.get("yes_price"))
    quantity = _decimal(payload.get("count_fp"))
    if quantity is None:
        quantity = _decimal(payload.get("count"))
    if (
        not trade_id
        or created_at is None
        or maker_side is None
        or yes_price is None
        or quantity is None
        or quantity <= 0
        or not Decimal(0) <= yes_price <= Decimal(1)
    ):
        return None
    return PublicAggressorTrade(
        trade_id=trade_id,
        created_at=created_at,
        maker_side=maker_side,
        yes_price=yes_price,
        quantity=quantity,
    )


def apply_volume_claims(
    trades: list[PublicAggressorTrade],
    claimed_by_trade: dict[str, float],
) -> list[PublicAggressorTrade]:
    """Subtract volume already claimed by earlier fills from each trade.

    A public trade's quantity is finite across the WHOLE lifetime of the
    market, not per monitor pass: once an order filled from it (and persisted
    its claims), later passes must only see the residual. Trades fully
    consumed are dropped.
    """

    if not claimed_by_trade:
        return list(trades)
    remaining: list[PublicAggressorTrade] = []
    for trade in trades:
        claimed = _decimal(claimed_by_trade.get(trade.trade_id, 0.0)) or Decimal(0)
        residual = trade.quantity - max(Decimal(0), claimed)
        if residual <= 0:
            continue
        remaining.append(
            PublicAggressorTrade(
                trade_id=trade.trade_id,
                created_at=trade.created_at,
                maker_side=trade.maker_side,
                yes_price=trade.yes_price,
                quantity=residual,
            )
        )
    return remaining


def _priority(order: RestingMakerOrder) -> tuple[Decimal, datetime, int]:
    # Better (higher) bid first, then time priority, then a stable id tiebreak.
    return (-order.limit_price, order.placed_at, order.order_id)


def allocate_maker_fills(
    trades: list[PublicAggressorTrade],
    orders: list[RestingMakerOrder],
) -> dict[int, OrderAllocation]:
    """Allocate each trade's volume once across compatible resting orders.

    For every normalized trade, compatible orders (same maker side, placed
    strictly earlier, limit at/through the traded price) consume the trade's
    residual volume in price-time priority: first each order's own estimated
    queue ahead, then order quantity. Volume spent on one order is never
    reused for another -- conservation is structural, not asserted.
    """

    ordered_trades = sorted(trades, key=lambda trade: (trade.created_at, trade.trade_id))
    ordered_orders = sorted(orders, key=_priority)
    queue_remaining = {
        order.order_id: max(Decimal(0), order.queue_ahead) for order in ordered_orders
    }
    unfilled = {order.order_id: order.quantity for order in ordered_orders}
    fills: dict[int, list[AllocatedFill]] = {order.order_id: [] for order in ordered_orders}

    for trade in ordered_trades:
        residual = trade.quantity
        for order in ordered_orders:
            if residual <= 0:
                break
            if order.side != trade.maker_side:
                continue
            if trade.created_at <= order.placed_at:
                continue
            if trade.side_price(order.side) > order.limit_price:
                continue
            if unfilled[order.order_id] <= 0:
                continue
            queue_take = min(queue_remaining[order.order_id], residual)
            queue_remaining[order.order_id] -= queue_take
            residual -= queue_take
            if residual <= 0 or queue_remaining[order.order_id] > 0:
                if queue_take > 0:
                    fills[order.order_id].append(
                        AllocatedFill(
                            order_id=order.order_id,
                            trade_id=trade.trade_id,
                            quantity=Decimal(0),
                            price=trade.side_price(order.side),
                            queue_consumed=queue_take,
                        )
                    )
                continue
            fill_take = min(unfilled[order.order_id], residual)
            unfilled[order.order_id] -= fill_take
            residual -= fill_take
            fills[order.order_id].append(
                AllocatedFill(
                    order_id=order.order_id,
                    trade_id=trade.trade_id,
                    quantity=fill_take,
                    price=trade.side_price(order.side),
                    queue_consumed=queue_take,
                )
            )

    return {
        order.order_id: OrderAllocation(order=order, fills=tuple(fills[order.order_id]))
        for order in ordered_orders
    }
