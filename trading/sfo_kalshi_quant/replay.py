"""Chronological, no-lookahead paper-account replay.

The engine is intentionally small and deterministic.  Resting maker orders fill
only from a later public trade at/through the limit after estimated queue ahead
has traded.  Every public trade carries exactly one aggressor direction and its
volume is allocated once across compatible resting orders in price-time
priority (audit EX-01), sharing the semantics of the live monitor's allocator.
Expiry, reservations, cash, fees, and settlement are replayed in timestamp
order; missing evidence leaves the order unfilled.
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
from .account import ACCOUNTING_POLICY_VERSION, SHARED_ACCOUNT_ID
from .backtest_rescore import _day_clustered_roi_ci
from .config import normalize_risk_profile_name
from .execution import initial_queue_ahead
from .logical_positions import LogicalPaperPosition, group_logical_positions
from .maker_fills import (
    EXECUTION_MODEL_VERSION,
    maker_trade_reaches_price,
    normalize_public_trade,
    uses_current_maker_semantics,
)
from .restatement import VERIFIED, restate
from .settlement_truth import (
    normalize_settlement_truth,
    row_resolves_yes as _resolves_yes,
    settlement_for_market,
)

# The maker side a public trade fills, keyed by the taker's book side
# (docs.kalshi.com/getting_started/order_direction): a bid-side taker bought
# YES and fills resting NO bids; an ask-side taker fills resting YES bids.
_MAKER_SIDE_BY_TAKER_BOOK_SIDE = {"bid": "NO", "ask": "YES"}


def _positive_integral_id(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (
        int(parsed)
        if math.isfinite(parsed) and parsed >= 1 and parsed.is_integer()
        else None
    )


def _finite_probability(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and 0 <= parsed <= 1 else None


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
    queue_price: float | None = None

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
    # Targeted exits (partial closes) name the position they reduce; an exit
    # without exit_order_id keeps the legacy close-every-ticker-position
    # behavior for hand-built event streams.
    exit_order_id: str | None = None


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
    # Require a provable complementary aggressor: the trade's taker book side
    # must imply the order's maker side. (Field name kept for compatibility.)
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
    queue_remaining: dict[str, float] = {}
    allocated: dict[str, float] = {}
    position_remaining: dict[str, float] = {}
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
                queue_remaining[order.order_id] = max(
                    0.0, model.queue_fraction * order.queue_ahead
                )
                allocated[order.order_id] = 0.0
                audit.append(_audit(now, "RESERVE", order.order_id, order.cost))
            continue

        if event.kind == "trade":
            maker_side = _MAKER_SIDE_BY_TAKER_BOOK_SIDE.get(
                (event.taker_book_side or "").lower()
            )
            # One trade, one aggressor, finite volume: consume it once across
            # compatible orders in price-time priority, queue ahead first.
            residual = max(0.0, event.quantity)
            eligible = sorted(
                (
                    (order_id, order)
                    for order_id, order in state.open_orders.items()
                    if order.ticker == event.ticker and now > as_utc(order.placed_at)
                ),
                key=lambda pair: (-pair[1].limit_price, as_utc(pair[1].placed_at), pair[0]),
            )
            for order_id, order in eligible:
                if residual <= 0:
                    break
                if model.require_bid_taker and maker_side != order.side:
                    continue
                if event.side != order.side or event.price is None:
                    continue
                fills_order = maker_trade_reaches_price(
                    event.price, order.limit_price
                )
                current_queue = queue_remaining.get(order_id, 0.0)
                if current_queue > 0:
                    queue_price = (
                        order.queue_price
                        if order.queue_price is not None
                        else order.limit_price
                    )
                    if not maker_trade_reaches_price(event.price, queue_price):
                        continue
                    queue_take = min(current_queue, residual)
                    queue_remaining[order_id] = current_queue - queue_take
                    residual -= queue_take
                    if residual <= 0 or queue_remaining[order_id] > 0:
                        continue
                if not fills_order:
                    continue
                fill_take = min(order.contracts - allocated.get(order_id, 0.0), residual)
                allocated[order_id] = allocated.get(order_id, 0.0) + fill_take
                residual -= fill_take
                if allocated[order_id] + 1e-12 < order.contracts:
                    continue
                state.reservations.pop(order_id, None)
                state.open_orders.pop(order_id, None)
                state.cash -= order.cost
                state.positions[order_id] = order
                filled += 1
                audit.append(_audit(now, "FILL_MAKER", order_id, order.cost))
            continue

        if event.kind == "exit":
            if event.price is None:
                continue
            if event.exit_order_id is not None:
                # Targeted, quantity-aware exit: a partial close reduces only
                # its own position by the executed lot; the remainder stays
                # open for later exits or settlement.
                order = state.positions.get(event.exit_order_id)
                if order is None or order.ticker != event.ticker:
                    continue
                held = position_remaining.get(event.exit_order_id, order.contracts)
                quantity = held if event.quantity <= 0 else min(event.quantity, held)
                if quantity <= 0:
                    continue
                cost_per_contract = order.cost / order.contracts if order.contracts else 0.0
                proceeds = quantity * event.price
                pnl = proceeds - quantity * cost_per_contract
                state.cash += proceeds
                state.realized_pnl += pnl
                day = now.date().isoformat()
                daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
                position_remaining[event.exit_order_id] = held - quantity
                if position_remaining[event.exit_order_id] <= 1e-9:
                    state.positions.pop(event.exit_order_id, None)
                settled += 1
                audit.append(_audit(now, "EXIT", event.exit_order_id, pnl))
                continue
            for order_id, order in list(state.positions.items()):
                if order.ticker != event.ticker:
                    continue
                held = position_remaining.get(order_id, order.contracts)
                cost_per_contract = order.cost / order.contracts if order.contracts else 0.0
                proceeds = held * event.price
                pnl = proceeds - held * cost_per_contract
                state.cash += proceeds
                state.realized_pnl += pnl
                day = now.date().isoformat()
                daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
                state.positions.pop(order_id, None)
                position_remaining.pop(order_id, None)
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
                held = position_remaining.get(order_id, order.contracts)
                won = event.resolved_yes if order.side == "YES" else not event.resolved_yes
                proceeds = held if won else 0.0
                cost_per_contract = order.cost / order.contracts if order.contracts else 0.0
                pnl = proceeds - held * cost_per_contract
                state.cash += proceeds
                state.realized_pnl += pnl
                daily_pnl[event.target_date] = daily_pnl.get(event.target_date, 0.0) + pnl
                state.positions.pop(order_id, None)
                position_remaining.pop(order_id, None)
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
            all_orders = conn.execute(
                "SELECT * FROM paper_orders ORDER BY created_at, id"
            ).fetchall()
            v4_allocation_owner_ids = {
                order_id
                for row in conn.execute(
                    "SELECT DISTINCT order_id FROM paper_maker_allocations "
                    "WHERE execution_model_version=?",
                    (EXECUTION_MODEL_VERSION,),
                ).fetchall()
                if (order_id := _positive_integral_id(row[0])) is not None
            } if _table_exists(conn, "paper_maker_allocations") else set()
            claim_owner_ids = {
                order_id
                for row in conn.execute(
                    "SELECT DISTINCT order_id FROM maker_volume_claims"
                ).fetchall()
                if (order_id := _positive_integral_id(row[0])) is not None
            } if _table_exists(conn, "maker_volume_claims") else set()
            entry_fill_rows = (
                conn.execute(
                    "SELECT order_id, details_json, idempotency_key "
                    "FROM paper_account_ledger WHERE event_type='ENTRY_FILL'"
                ).fetchall()
                if _table_exists(conn, "paper_account_ledger")
                else []
            )
            current_entry_fill_owner_ids = {
                order_id
                for row in entry_fill_rows
                if (order_id := _positive_integral_id(row["order_id"]))
                is not None
                and (
                    _json_object(row["details_json"]).get(
                        "execution_model_version"
                    )
                    == EXECUTION_MODEL_VERSION
                    or EXECUTION_MODEL_VERSION
                    in str(row["idempotency_key"] or "")
                )
            }
            plausible_current_maker_ids = {
                order_id
                for row in all_orders
                if str(row["status"] or "") != "REJECTED"
                and (
                    order_id := _positive_integral_id(row["id"])
                )
                is not None
                and str(_row_value(row, "entry_mode") or "") == "limit"
                and _finite_probability(_row_value(row, "limit_price"))
                is not None
                and (
                    str(_row_value(row, "execution_model_version") or "")
                    == EXECUTION_MODEL_VERSION
                    or order_id in current_entry_fill_owner_ids
                )
            }
            trades = (
                conn.execute(
                    "SELECT * FROM dataset_kalshi_trades ORDER BY created_time, trade_id"
                ).fetchall()
                if _table_exists(conn, "dataset_kalshi_trades")
                else []
            )
            # The explicit execution transition marks when this database first
            # ran the current execution/accounting semantics. Only live rows
            # written after it can count;
            # only trading days entirely after it count toward the promotion
            # clock (audit Batch D: reset at the version boundary).
            semantics_boundary = None
            if _table_exists(conn, "paper_account_ledger"):
                boundary_row = conn.execute(
                    "SELECT MIN(created_at) FROM paper_account_ledger "
                    "WHERE event_type = 'EXECUTION_SEMANTICS_TRANSITION' "
                    "AND idempotency_key = ?",
                    (f"execution:{EXECUTION_MODEL_VERSION}",),
                ).fetchone()
                semantics_boundary = boundary_row[0] if boundary_row else None
            event_orders = [
                row
                for row in all_orders
                if str(row["status"]) != "REJECTED"
                and semantics_boundary
                and str(_row_value(row, "account_id") or "") == SHARED_ACCOUNT_ID
                and str(_row_value(row, "execution_model_version") or "")
                == EXECUTION_MODEL_VERSION
                and str(row["created_at"] or "") >= str(semantics_boundary)
            ]
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    try:
        restated_orders = restate(Path(db_path)).get("orders", [])
        verified_order_ids = {
            int(row["order_id"])
            for row in restated_orders
            if row.get("verification") == VERIFIED
        }
    except (OSError, sqlite3.Error, TypeError, ValueError):
        verified_order_ids = set()
    # Maker-v4 events consume only evidence that survived immutable
    # restatement. Legacy/taker rows retain their historical replay behavior;
    # their independent verification gates are applied to readiness below.
    event_orders = [
        row
        for row in event_orders
        if _json_object(_row_value(row, "fill_evidence_json")).get("model")
        != "maker_allocator_price_time_v4"
        or int(row["id"]) in verified_order_ids
    ]
    # A partial-close child is one lot of its root decision, not an independent
    # replay candidate. If any lot fails immutable restatement, exclude the
    # complete maker-v4 decision so its siblings cannot contribute money or
    # event counts on their own.
    invalid_logical_lot_ids: set[int] = set()
    for group in group_logical_positions(all_orders):
        root_id = _positive_integral_id(group.root.get("id"))
        row_generation_is_current = (
            str(group.root.get("execution_model_version") or "")
            == EXECUTION_MODEL_VERSION
        )
        has_current_maker_authority = (
            uses_current_maker_semantics(
                group.root.get("execution_model_version"),
                group.root.get("entry_mode"),
                group.root.get("fill_model"),
            )
            or root_id in v4_allocation_owner_ids
            or (row_generation_is_current and root_id in claim_owner_ids)
            or root_id in plausible_current_maker_ids
        )
        if not has_current_maker_authority:
            continue
        lot_ids = {
            int(lot["id"])
            for lot in group.lots
            if isinstance(lot.get("id"), int)
            or str(lot.get("id") or "").isdigit()
        }
        if not group.valid or not lot_ids or not lot_ids.issubset(
            verified_order_ids
        ):
            invalid_logical_lot_ids.update(lot_ids)
    event_orders = [
        row for row in event_orders if int(row["id"]) not in invalid_logical_lot_ids
    ]

    truth = normalize_settlement_truth(settlements)
    events: list[ReplayEvent] = []
    legacy_orders = 0
    settlement_rows: dict[tuple[str, str], sqlite3.Row] = {}
    # Partial-close lots (audit EX-02) are exits of their parent position, not
    # independent orders: replay the parent at its ORIGINAL size and reduce it
    # with a targeted exit event per executed lot.
    child_quantity_by_parent: dict[str, float] = {}
    for row in event_orders:
        parent_id = _row_value(row, "parent_order_id")
        if parent_id:
            key = str(int(parent_id))
            child_quantity_by_parent[key] = (
                child_quantity_by_parent.get(key, 0.0) + float(row["contracts"] or 0.0)
            )
    for row in event_orders:
        parent_id = _row_value(row, "parent_order_id")
        if parent_id:
            closed_at = _parse_time(row["closed_at"])
            if closed_at is not None and row["exit_price"] is not None:
                net_exit = float(row["exit_price"]) - float(row["exit_fee_per_contract"] or 0.0)
                events.append(
                    ReplayEvent(
                        closed_at,
                        "exit",
                        str(row["market_ticker"]),
                        price=net_exit,
                        quantity=float(row["contracts"] or 0.0),
                        exit_order_id=str(int(parent_id)),
                    )
                )
            continue
        placed = _parse_time(row["created_at"])
        if placed is None:
            continue
        entry_mode = str(_row_value(row, "entry_mode") or "market")
        fill_model = str(_row_value(row, "fill_model") or "")
        immediate = entry_mode != "limit" or fill_model == "immediate_visible_quote"
        fingerprint = _row_value(row, "strategy_fingerprint")
        if not fingerprint or fingerprint == "legacy_independent_sizing":
            legacy_orders += 1
        remaining_contracts = float(row["contracts"] or 0.0)
        original_contracts = remaining_contracts + child_quantity_by_parent.get(
            str(row["id"]), 0.0
        )
        limit_price = float(
            _row_value(row, "limit_price") or row["entry_price"] or row["yes_ask"]
        )
        order = ReplayOrder(
            order_id=str(row["id"]),
            placed_at=placed,
            target_date=str(row["target_date"]),
            ticker=str(row["market_ticker"]),
            side=str(row["side"] or "YES").upper(),
            limit_price=limit_price,
            contracts=original_contracts,
            fee_per_contract=float(row["fee_per_contract"] or 0.0),
            queue_ahead=initial_queue_ahead(
                limit_price,
                (
                    float(row["entry_bid"])
                    if row["entry_bid"] is not None
                    else None
                ),
                float(row["entry_bid_size"] or 0.0),
            ),
            ttl_minutes=15,
            immediate=immediate,
            queue_price=(
                float(row["entry_bid"])
                if row["entry_bid"] is not None
                else limit_price
            ),
        )
        events.append(ReplayEvent(placed, "order", order.ticker, order=order))
        closed_at = _parse_time(row["closed_at"])
        if closed_at is not None and row["exit_price"] is not None:
            net_exit = float(row["exit_price"]) - float(row["exit_fee_per_contract"] or 0.0)
            events.append(
                ReplayEvent(
                    closed_at,
                    "exit",
                    order.ticker,
                    price=net_exit,
                    quantity=remaining_contracts,
                    exit_order_id=order.order_id,
                )
            )
        if str(row["status"]) == "PAPER_SETTLED" and row["settled_at"]:
            settlement_rows[(order.ticker, order.target_date)] = row

    for index, row in enumerate(trades):
        occurred = _parse_time(row["created_time"])
        try:
            is_block_trade = int(row["is_block_trade"] or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if occurred is None or is_block_trade not in {0, 1} or is_block_trade:
            continue
        raw = _json_object(row["raw_json"])
        normalized = normalize_public_trade(
            {
                "trade_id": raw.get("trade_id")
                or f"dataset-{row['ticker']}-{row['created_time']}-{index}",
                "created_time": row["created_time"],
                "taker_book_side": raw.get("taker_book_side"),
                "taker_outcome_side": raw.get("taker_outcome_side") or raw.get("taker_side"),
                "yes_price": row["yes_price"],
                "count": row["count"],
            }
        )
        if normalized is None:
            # A trade that cannot prove its aggressor direction, price, or
            # quantity must not create replay fills (do not invent fills).
            continue
        events.append(
            ReplayEvent(
                occurred,
                "trade",
                str(row["ticker"]),
                side=normalized.maker_side,
                price=float(normalized.side_price(normalized.maker_side)),
                quantity=float(normalized.quantity),
                taker_book_side="bid" if normalized.maker_side == "NO" else "ask",
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
    eligible_root_ids = _eligible_readiness_root_ids(
        all_orders, semantics_boundary
    )
    reasons = set(result["promotion_block_reasons"])
    if legacy_orders:
        reasons.add(f"{legacy_orders} legacy orders lack a current strategy fingerprint")
    if not trades:
        reasons.add("no persisted public trade events for maker fill validation")
    # Promotion-clock reset at the semantics boundary: resolved trading days
    # count only when every order of that day was placed under the corrected
    # execution/accounting semantics and its execution evidence restates as
    # verified under those semantics.
    resolved_days: dict[str, bool] = {}
    unverified_live_decisions: set[int] = set()
    for group in group_logical_positions(all_orders):
        root = group.root
        profile_class = _readiness_root_profile_class(root)
        if (
            profile_class == "research"
            and group.valid
            and _readiness_group_has_consistent_scope(
                group, semantics_boundary
            )
        ):
            continue
        if str(root["status"]) not in ("PAPER_SETTLED", "PAPER_CLOSED"):
            continue
        execution_verified = bool(group.resolved_lots) and all(
            int(row["id"]) in verified_order_ids for row in group.resolved_lots
        )
        if (
            profile_class == "live"
            and semantics_boundary
            and (
                _parse_time(root["created_at"]) is None
                or str(root["created_at"] or "") >= semantics_boundary
            )
            and not execution_verified
        ):
            unverified_live_decisions.add(group.logical_order_id)
        day = str(root["target_date"])
        qualified = (
            profile_class == "live"
            and group.logical_order_id in eligible_root_ids
            and group.terminal
            and _readiness_group_has_consistent_scope(
                group, semantics_boundary
            )
            and execution_verified
        )
        resolved_days[day] = resolved_days.get(day, True) and qualified
    post_boundary_days = sum(1 for qualified in resolved_days.values() if qualified)
    if unverified_live_decisions:
        reasons.add(
            f"{len(unverified_live_decisions)} resolved live decision(s) have "
            "unverified execution evidence"
        )
    if post_boundary_days < 30:
        reasons.add(
            f"only {post_boundary_days} independent trading days under "
            f"{EXECUTION_MODEL_VERSION}/{ACCOUNTING_POLICY_VERSION} (need 30); "
            "promotion clock restarted at the corrected-semantics boundary"
        )
    result["promotion_block_reasons"] = sorted(reasons)
    result["promotion_eligible"] = not reasons
    result["available"] = True
    result["source_orders"] = len(event_orders)
    result["source_trades"] = len(trades)
    result["execution_model_version"] = EXECUTION_MODEL_VERSION
    result["accounting_policy_version"] = ACCOUNTING_POLICY_VERSION
    result["semantics_boundary"] = semantics_boundary
    result["post_boundary_days"] = post_boundary_days
    result["verified_decisions"] = len(
        _verified_resolved_decision_groups(
            all_orders,
            verified_order_ids,
            eligible_root_ids=eligible_root_ids,
            semantics_boundary=semantics_boundary,
        )
    )
    result["evidence_scope"] = {
        "account_id": SHARED_ACCOUNT_ID,
        "execution_model_version": EXECUTION_MODEL_VERSION,
        "accounting_policy_version": ACCOUNTING_POLICY_VERSION,
        "pre_boundary_excluded": True,
        "research_excluded": True,
    }
    result["readiness_metrics"] = _post_boundary_readiness_metrics(
        all_orders,
        promotion_eligible=bool(result["promotion_eligible"]),
        semantics_boundary=semantics_boundary,
        initial_capital=initial_capital,
        verified_order_ids=verified_order_ids,
        eligible_root_ids=eligible_root_ids,
    )
    return result


def _post_boundary_readiness_metrics(
    orders: list[sqlite3.Row],
    *,
    promotion_eligible: bool,
    semantics_boundary: str | None,
    initial_capital: float,
    verified_order_ids: set[int],
    eligible_root_ids: set[int],
) -> dict[str, object]:
    """Economic readiness inputs from chronological post-boundary live rows."""

    groups = _verified_resolved_decision_groups(
        orders,
        verified_order_ids,
        eligible_root_ids=eligible_root_ids,
        semantics_boundary=semantics_boundary,
    )
    resolved: list[dict[str, object]] = []
    for root_id, lots in groups.items():
        root = next(row for row in lots if int(row["id"]) == root_id)
        resolved.append(
            {
                "root_order_id": root_id,
                "target_date": str(root["target_date"]),
                "side": str(root["side"] or "YES").upper(),
                "realized_pnl": sum(float(row["realized_pnl"] or 0.0) for row in lots),
                "capital_at_risk": sum(
                    float(row["contracts"] or 0.0)
                    * float(row["cost_per_contract"] or 0.0)
                    for row in lots
                ),
                "lots": len(lots),
            }
        )
    per_day: dict[str, dict[str, float]] = {}
    by_side_rows: dict[str, list[dict[str, object]]] = {}
    for decision in resolved:
        day = str(decision["target_date"])
        bucket = per_day.setdefault(day, {"pnl": 0.0, "capital": 0.0})
        bucket["pnl"] += float(decision["realized_pnl"])
        bucket["capital"] += float(decision["capital_at_risk"])
        by_side_rows.setdefault(str(decision["side"]), []).append(decision)
    total_pnl = sum(day["pnl"] for day in per_day.values())
    total_capital = sum(day["capital"] for day in per_day.values())
    equity = peak = initial_capital
    max_drawdown = 0.0
    for day in sorted(per_day):
        equity += per_day[day]["pnl"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak > 0 else 0.0)
    growth = _daily_log_growth(
        initial_capital, {day: values["pnl"] for day, values in per_day.items()}
    )
    ci = _day_clustered_roi_ci(per_day, samples=2000, seed=0)

    def side_bucket(rows: list[dict[str, object]]) -> dict[str, object]:
        capital = sum(float(row["capital_at_risk"]) for row in rows)
        pnl = sum(float(row["realized_pnl"]) for row in rows)
        return {
            "trades": len(rows),
            "independent_days": len({str(row["target_date"]) for row in rows}),
            "realized_pnl": round(pnl, 4),
            "capital_at_risk": round(capital, 4),
            "roi": round(pnl / capital, 6) if capital > 0 else None,
        }

    cohort = side_bucket(resolved)
    cohort["source"] = "post-boundary exec-v4 paper-shared chronological outcomes"
    return {
        "evidence_kind": "chronological_account_replay",
        "promotion_eligible": promotion_eligible,
        "config_basis": "post-boundary exec-v4 live evidence only",
        "semantics_boundary": semantics_boundary,
        "source_cohort": "post_exec_v4_live",
        "counts": {
            "settled_decisions": len(resolved),
            "independent_days": len(per_day),
        },
        "candidate": {
            "realized_pnl": round(total_pnl, 4),
            "capital_at_risk": round(total_capital, 4),
            "roi": round(total_pnl / total_capital, 6) if total_capital > 0 else None,
            "roi_ci95_day_clustered": (
                [round(ci[0], 6), round(ci[1], 6)] if ci is not None else None
            ),
            "log_growth_per_independent_day": (
                round(growth, 8) if growth is not None else None
            ),
            "max_drawdown_pct": round(max_drawdown, 6),
        },
        "by_forecast_cohort": {"post_exec_v4_live": cohort} if resolved else {},
        "by_cohort": {"post_exec_v4_live": cohort} if resolved else {},
        "by_side": {
            side: side_bucket(rows) for side, rows in sorted(by_side_rows.items())
        },
    }


def _eligible_readiness_root_ids(
    orders: list[sqlite3.Row],
    semantics_boundary: str | None,
) -> set[int]:
    """Select the logical roots eligible for post-boundary live evidence."""

    if not semantics_boundary:
        return set()
    eligible: set[int] = set()
    for group in group_logical_positions(orders):
        root = group.root
        if (
            str(_row_value(root, "account_id") or "") == SHARED_ACCOUNT_ID
            and str(_row_value(root, "execution_model_version") or "")
            == EXECUTION_MODEL_VERSION
            and str(root["created_at"] or "") >= semantics_boundary
            and _readiness_root_profile_class(root) == "live"
        ):
            eligible.add(group.logical_order_id)
    return eligible


def _readiness_root_profile_class(
    root: dict[str, object],
) -> Literal["live", "research", "invalid"]:
    """Classify root policy identity without consulting ambient defaults."""

    raw_profile = _row_value(root, "risk_profile")
    profile = (
        "live"
        if raw_profile is None or not str(raw_profile).strip()
        else str(raw_profile)
    )
    try:
        normalized = normalize_risk_profile_name(profile)
    except (AttributeError, TypeError, ValueError):
        return "invalid"
    return "research" if normalized == "research" else "live"


def _readiness_group_has_consistent_scope(
    group: LogicalPaperPosition,
    semantics_boundary: str | None,
) -> bool:
    """Require every lot to preserve its eligible root's evidence scope."""

    if not semantics_boundary:
        return False
    root_account = str(_row_value(group.root, "account_id") or "")
    root_version = str(
        _row_value(group.root, "execution_model_version") or ""
    )
    return all(
        str(_row_value(lot, "account_id") or "") == root_account
        and str(_row_value(lot, "execution_model_version") or "")
        == root_version
        and str(lot["created_at"] or "") >= semantics_boundary
        for lot in group.lots
    )


def _verified_resolved_decision_groups(
    orders: list[sqlite3.Row],
    verified_order_ids: set[int],
    *,
    eligible_root_ids: set[int] | None = None,
    semantics_boundary: str | None = None,
) -> dict[int, list[dict[str, object]]]:
    """Group immutable partial-close lots into their originating decision."""

    verified: dict[int, list[dict[str, object]]] = {}
    for group in group_logical_positions(orders):
        if (
            eligible_root_ids is not None
            and group.logical_order_id not in eligible_root_ids
        ):
            continue
        if (
            eligible_root_ids is not None
            and not _readiness_group_has_consistent_scope(
                group, semantics_boundary
            )
        ):
            continue
        if not group.terminal:
            continue
        lots = list(group.resolved_lots)
        if not lots:
            continue
        if all(int(row["id"]) in verified_order_ids for row in lots):
            verified[group.logical_order_id] = lots
    return verified


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
