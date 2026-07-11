"""Chronological, no-lookahead paper-account replay.

The engine is intentionally small and deterministic.  Resting maker orders fill
only from a later public trade at/through the limit after estimated queue ahead
has traded.  Expiry, reservations, cash, fees, and settlement are replayed in
timestamp order; missing evidence leaves the order unfilled.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from ._util import _json_object, _row_value, _table_exists
from .settlement_truth import (
    normalize_settlement_truth,
    row_resolves_yes as _resolves_yes,
    settlement_for_market,
)


@dataclass(frozen=True)
class ReplayOrder:
    order_id: str
    placed_at: datetime
    target_date: str
    ticker: str
    side: Literal["YES", "NO"]
    limit_price: float
    contracts: float
    fee_per_contract: float
    queue_ahead: float = 0.0
    ttl_minutes: int = 15
    immediate: bool = False

    @property
    def cost(self) -> float:
        return self.contracts * (self.limit_price + self.fee_per_contract)


@dataclass(frozen=True)
class ReplayEvent:
    occurred_at: datetime
    kind: Literal["order", "trade", "exit", "settlement"]
    ticker: str
    order: ReplayOrder | None = None
    side: Literal["YES", "NO"] | None = None
    price: float | None = None
    quantity: float = 0.0
    taker_book_side: str | None = None
    target_date: str | None = None
    resolved_yes: bool | None = None


@dataclass
class ReplayAccountState:
    initial_capital: float = 1000.0
    cash: float = 1000.0
    reservations: dict[str, float] = field(default_factory=dict)
    open_orders: dict[str, ReplayOrder] = field(default_factory=dict)
    positions: dict[str, ReplayOrder] = field(default_factory=dict)
    realized_pnl: float = 0.0

    @property
    def reserved_cash(self) -> float:
        return sum(self.reservations.values())

    @property
    def available_cash(self) -> float:
        return self.cash - self.reserved_cash

    @property
    def realized_equity(self) -> float:
        return self.initial_capital + self.realized_pnl


@dataclass(frozen=True)
class ExecutionModel:
    require_later_trade: bool = True
    require_bid_taker: bool = True
    queue_fraction: float = 1.0


@dataclass(frozen=True)
class ReplayResult:
    evidence_kind: str
    promotion_eligible: bool
    promotion_block_reasons: tuple[str, ...]
    placed: int
    filled: int
    cancelled: int
    settled: int
    ending_cash: float
    ending_realized_equity: float
    realized_pnl: float
    daily_log_growth: float | None
    events: tuple[dict[str, object], ...]


def run_replay(
    events: list[ReplayEvent],
    *,
    initial_capital: float = 1000.0,
    execution_model: ExecutionModel | None = None,
) -> ReplayResult:
    model = execution_model or ExecutionModel()
    state = ReplayAccountState(initial_capital=initial_capital, cash=initial_capital)
    audit: list[dict[str, object]] = []
    queue_traded: dict[str, float] = {}
    placed = filled = cancelled = settled = 0
    block_reasons: set[str] = set()
    daily_pnl: dict[str, float] = {}

    normalized = sorted(events, key=lambda event: (as_utc(event.occurred_at), _kind_order(event.kind)))
    last_time: datetime | None = None
    for event in normalized:
        now = as_utc(event.occurred_at)
        if last_time is not None and now < last_time:
            raise ValueError("replay events moved backward in time")
        _expire_orders(state, now, audit)
        last_time = now

        if event.kind == "order":
            order = event.order
            if order is None or as_utc(order.placed_at) != now:
                raise ValueError("order event timestamp must equal order placement timestamp")
            placed += 1
            if order.cost > state.available_cash + 1e-9:
                audit.append(_audit(now, "REJECT_CASH", order.order_id, order.cost))
                continue
            if order.immediate:
                state.cash -= order.cost
                state.positions[order.order_id] = order
                filled += 1
                audit.append(_audit(now, "FILL_TAKER", order.order_id, order.cost))
            else:
                state.reservations[order.order_id] = order.cost
                state.open_orders[order.order_id] = order
                audit.append(_audit(now, "RESERVE", order.order_id, order.cost))
            continue

        if event.kind == "trade":
            for order_id, order in list(state.open_orders.items()):
                if order.ticker != event.ticker or now <= as_utc(order.placed_at):
                    continue
                if model.require_bid_taker and (event.taker_book_side or "").lower() != "bid":
                    continue
                if event.side != order.side or event.price is None or event.price > order.limit_price + 1e-12:
                    continue
                queue_traded[order_id] = queue_traded.get(order_id, 0.0) + max(0.0, event.quantity)
                required = model.queue_fraction * order.queue_ahead + order.contracts
                if queue_traded[order_id] + 1e-12 < required:
                    continue
                state.reservations.pop(order_id, None)
                state.open_orders.pop(order_id, None)
                state.cash -= order.cost
                state.positions[order_id] = order
                filled += 1
                audit.append(_audit(now, "FILL_MAKER", order_id, order.cost))
            continue

        if event.kind == "exit":
            for order_id, order in list(state.positions.items()):
                if order.ticker != event.ticker or event.price is None:
                    continue
                proceeds = order.contracts * event.price
                pnl = proceeds - order.cost
                state.cash += proceeds
                state.realized_pnl += pnl
                day = now.date().isoformat()
                daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
                state.positions.pop(order_id, None)
                settled += 1
                audit.append(_audit(now, "EXIT", order_id, pnl))
            continue

        if event.kind == "settlement":
            if event.target_date is None or event.resolved_yes is None:
                raise ValueError("settlement event requires target_date and resolved_yes")
            matching = [
                (order_id, order) for order_id, order in state.positions.items()
                if order.ticker == event.ticker and order.target_date == event.target_date
            ]
            if not matching:
                block_reasons.add("settlement without a filled replay position")
            for order_id, order in matching:
                won = event.resolved_yes if order.side == "YES" else not event.resolved_yes
                proceeds = order.contracts if won else 0.0
                pnl = proceeds - order.cost
                state.cash += proceeds
                state.realized_pnl += pnl
                daily_pnl[event.target_date] = daily_pnl.get(event.target_date, 0.0) + pnl
                state.positions.pop(order_id, None)
                settled += 1
                audit.append(_audit(now, "SETTLE", order_id, pnl))

    if last_time is not None:
        before = len(state.open_orders)
        _expire_orders(state, last_time + timedelta(days=1), audit)
        cancelled += before - len(state.open_orders)
    cancelled += sum(1 for row in audit if row["event"] == "CANCEL_TTL") - cancelled
    if state.positions:
        block_reasons.add("filled positions missing authoritative settlement")
    if filled == 0:
        block_reasons.add("no orders filled with conservative evidence")
    if len(daily_pnl) < 30:
        block_reasons.add("fewer than 30 independent replay days")
    growth = _daily_log_growth(initial_capital, daily_pnl)
    return ReplayResult(
        evidence_kind="chronological_account_replay",
        promotion_eligible=not block_reasons,
        promotion_block_reasons=tuple(sorted(block_reasons)),
        placed=placed,
        filled=filled,
        cancelled=cancelled,
        settled=settled,
        ending_cash=round(state.cash, 4),
        ending_realized_equity=round(state.realized_equity, 4),
        realized_pnl=round(state.realized_pnl, 4),
        daily_log_growth=round(growth, 8) if growth is not None else None,
        events=tuple(audit),
    )


def replay_from_database(
    db_path,
    settlements,
    *,
    initial_capital: float = 1000.0,
) -> dict[str, object]:
    """Build replay events from persisted orders/trades and authoritative truth."""

    if not Path(db_path).exists():
        return {"available": False, "reason": f"paper database not found: {db_path}"}
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not _table_exists(conn, "paper_orders"):
                return {"available": False, "reason": "paper_orders table missing"}
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE status != 'REJECTED' ORDER BY created_at, id"
            ).fetchall()
            trades = (
                conn.execute(
                    "SELECT * FROM dataset_kalshi_trades ORDER BY created_time, trade_id"
                ).fetchall()
                if _table_exists(conn, "dataset_kalshi_trades")
                else []
            )
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    truth = normalize_settlement_truth(settlements)
    events: list[ReplayEvent] = []
    legacy_orders = 0
    settlement_rows: dict[tuple[str, str], sqlite3.Row] = {}
    for row in orders:
        placed = _parse_time(row["created_at"])
        if placed is None:
            continue
        entry_mode = str(_row_value(row, "entry_mode") or "market")
        fill_model = str(_row_value(row, "fill_model") or "")
        immediate = entry_mode != "limit" or fill_model == "immediate_visible_quote"
        fingerprint = _row_value(row, "strategy_fingerprint")
        if not fingerprint or fingerprint == "legacy_independent_sizing":
            legacy_orders += 1
        order = ReplayOrder(
            order_id=str(row["id"]),
            placed_at=placed,
            target_date=str(row["target_date"]),
            ticker=str(row["market_ticker"]),
            side=str(row["side"] or "YES").upper(),
            limit_price=float(_row_value(row, "limit_price") or row["entry_price"] or row["yes_ask"]),
            contracts=float(row["contracts"] or 0.0),
            fee_per_contract=float(row["fee_per_contract"] or 0.0),
            queue_ahead=float(row["entry_bid_size"] or 0.0),
            ttl_minutes=15,
            immediate=immediate,
        )
        events.append(ReplayEvent(placed, "order", order.ticker, order=order))
        closed_at = _parse_time(row["closed_at"])
        if closed_at is not None and row["exit_price"] is not None:
            net_exit = float(row["exit_price"]) - float(row["exit_fee_per_contract"] or 0.0)
            events.append(ReplayEvent(closed_at, "exit", order.ticker, price=net_exit))
        if str(row["status"]) == "PAPER_SETTLED" and row["settled_at"]:
            settlement_rows[(order.ticker, order.target_date)] = row

    for row in trades:
        occurred = _parse_time(row["created_time"])
        if occurred is None or int(row["is_block_trade"] or 0):
            continue
        raw = _json_object(row["raw_json"])
        taker_book_side = raw.get("taker_book_side")
        for side, column in (("YES", "yes_price"), ("NO", "no_price")):
            price = row[column]
            if price is None:
                continue
            events.append(
                ReplayEvent(
                    occurred,
                    "trade",
                    str(row["ticker"]),
                    side=side,
                    price=float(price),
                    quantity=float(row["count"] or 0.0),
                    taker_book_side=str(taker_book_side or ""),
                )
            )

    for (ticker, target_date), row in settlement_rows.items():
        high = settlement_for_market(truth, ticker, target_date)
        if high is None:
            continue
        settled_time = _parse_time(row["settled_at"] or row["closed_at"])
        if settled_time is None:
            settled_time = datetime.fromisoformat(target_date).replace(tzinfo=UTC) + timedelta(days=2)
        events.append(
            ReplayEvent(
                settled_time,
                "settlement",
                ticker,
                target_date=target_date,
                resolved_yes=_resolves_yes(row, float(high)),
            )
        )

    result = asdict(run_replay(events, initial_capital=initial_capital))
    reasons = set(result["promotion_block_reasons"])
    if legacy_orders:
        reasons.add(f"{legacy_orders} legacy orders lack a current strategy fingerprint")
    if not trades:
        reasons.add("no persisted public trade events for maker fill validation")
    result["promotion_block_reasons"] = sorted(reasons)
    result["promotion_eligible"] = not reasons
    result["available"] = True
    result["source_orders"] = len(orders)
    result["source_trades"] = len(trades)
    return result


def _expire_orders(state: ReplayAccountState, now: datetime, audit: list[dict[str, object]]) -> None:
    for order_id, order in list(state.open_orders.items()):
        expires = as_utc(order.placed_at) + timedelta(minutes=order.ttl_minutes)
        if expires > now:
            continue
        state.open_orders.pop(order_id, None)
        state.reservations.pop(order_id, None)
        audit.append(_audit(expires, "CANCEL_TTL", order_id, 0.0))


def _daily_log_growth(initial: float, daily_pnl: dict[str, float]) -> float | None:
    equity = initial
    logs: list[float] = []
    for day in sorted(daily_pnl):
        closing = equity + daily_pnl[day]
        if equity <= 0 or closing <= 0:
            return None
        logs.append(math.log(closing / equity))
        equity = closing
    return sum(logs) / len(logs) if logs else None


def _audit(at: datetime, event: str, order_id: str, amount: float) -> dict[str, object]:
    return {"at": as_utc(at).isoformat(), "event": event, "order_id": order_id, "amount": round(amount, 4)}


def _kind_order(kind: str) -> int:
    return {"order": 0, "trade": 1, "exit": 2, "settlement": 3}[kind]


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None
