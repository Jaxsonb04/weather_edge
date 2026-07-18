"""Read-only historical restatement under versioned execution semantics.

Audit 2026-07-13 Batch D: the original journal is immutable; this module
re-examines every order's ENTRY and EXIT evidence under the corrected
execution model (``EXECUTION_MODEL_VERSION``) and reports, side by side, the
legacy headline P&L and the verification-classed view. It never writes to the
database it inspects.

Classification rules (conservative -- insufficient evidence is UNVERIFIABLE,
never "filled by assumption"):

Entry evidence
  - ``maker_allocator_price_time_v4`` fill evidence: VERIFIED when its
    persisted queue-price allocation and public tape reproduce cleanly.
  - ``maker_allocator_price_time_v3`` evidence remains historical and is
    reported as pre-corrected queue semantics; it is never rewritten as v4.
  - ``maker_allocator_price_time_v2`` evidence has unreplayable queue state.
  - Legacy ``later_trade_at_or_through_with_queue_ahead`` evidence:
      * YES-side orders: the legacy filter demanded ``taker_book_side ==
        "bid"``, the WRONG aggressor direction for a resting YES bid --
        DIRECTION_INVALID.
      * NO-side orders: direction was coincidentally right, but volume was
        summed per order over the whole trade history, so the same public
        volume could credit several orders (production orders 226/227 and
        261/262) -- and with no persisted public tape the allocation cannot
        be reproduced. LEGACY_TAPE_UNREPLAYABLE, plus DOUBLE_CREDITED when
        the same trade id appears in another order's evidence.
  - Taker entries (``immediate_visible_quote``): entry at the displayed ask
    recorded in the quote snapshot -- VERIFIED entry price, sized against
    displayed liquidity only after the 2026-07-10 sizing fix.

Exit evidence
  - Settlement against verified final official truth: VERIFIED.
  - Closes carrying ``exit_execution`` (executed quantity + displayed depth):
    VERIFIED.
  - Legacy closes that booked every contract at the top bid with no recorded
    depth: EXIT_DEPTH_UNVERIFIED.

An order is VERIFIED only when both sides of its lifecycle are verified;
otherwise it is UNVERIFIABLE with the reason set preserved. All realized P&L
is reported in both views; nothing is rewritten.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ._util import _json_object
from .account import ACCOUNTING_POLICY_VERSION, RESEARCH_ACCOUNT_ID
from .execution import initial_queue_ahead
from .maker_fills import (
    EXECUTION_MODEL_VERSION,
    PublicAggressorTrade,
    RestingMakerOrder,
    allocate_maker_fills,
    apply_volume_claims,
    depth_observation_is_contemporaneous,
    normalize_public_trade,
)

# The taker-entry displayed-ask sizing cap landed 2026-07-10; taker entries
# before it could exceed displayed liquidity.
TAKER_SIZING_FIX_DATE = "2026-07-10"

VERIFIED = "VERIFIED"
UNVERIFIABLE = "UNVERIFIABLE"
_REPLAY_TOLERANCE = 1e-9


def _append_finding(findings: list[str], finding: str) -> None:
    if finding not in findings:
        findings.append(finding)


def _parse_replay_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _close_number(left: object, right: object) -> bool:
    parsed_left = _finite_number(left)
    parsed_right = _finite_number(right)
    if parsed_left is None or parsed_right is None:
        return False
    return abs(parsed_left - parsed_right) <= _REPLAY_TOLERANCE


def _finite_number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _positive_row_id(value: object) -> int | None:
    parsed = _finite_number(value, minimum=1)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _binary_flag(value: object) -> bool | None:
    parsed = _finite_number(value, minimum=0, maximum=1)
    if parsed is None or not parsed.is_integer():
        return None
    return bool(int(parsed))


def _optional_parent_id(value: object) -> tuple[int | None, bool]:
    """Return a parent id and whether a persisted non-null value was invalid."""

    if value is None:
        return None, False
    parsed = _finite_number(value, minimum=0)
    if parsed is not None and parsed.is_integer() and parsed == 0:
        return None, False
    parent_id = _positive_row_id(value)
    return parent_id, parent_id is None


def _order_replay_input_findings(row: sqlite3.Row) -> list[str]:
    findings: list[str] = []
    limit_value = row["limit_price"]
    if limit_value is None:
        limit_value = row["entry_price"]
    if _finite_number(limit_value, minimum=0, maximum=1) is None:
        findings.append("EXEC_V4_ORDER_LIMIT_PRICE_INVALID")
    if row["entry_bid"] is not None and _finite_number(
        row["entry_bid"], minimum=0, maximum=1
    ) is None:
        findings.append("EXEC_V4_ORDER_ENTRY_BID_INVALID")
    if row["entry_bid_size"] is not None and _finite_number(
        row["entry_bid_size"], minimum=0
    ) is None:
        findings.append("EXEC_V4_ORDER_ENTRY_BID_SIZE_INVALID")
    if _finite_number(row["requested_contracts"], minimum=0) in {None, 0.0}:
        findings.append("EXEC_V4_ORDER_REQUESTED_QUANTITY_INVALID")
    if _finite_number(row["filled_contracts"], minimum=0) is None:
        findings.append("EXEC_V4_ORDER_FILLED_QUANTITY_INVALID")
    if _parse_replay_time(row["created_at"]) is None:
        findings.append("EXEC_V4_ORDER_PLACED_AT_INVALID")
    return findings


def _allocation_replay_input_findings(row: sqlite3.Row) -> list[str]:
    findings: list[str] = []
    if _positive_row_id(row["order_id"]) is None:
        findings.append("EXEC_V4_ALLOCATION_ORDER_ID_INVALID")
    if not str(row["trade_id"] or "").strip():
        findings.append("EXEC_V4_ALLOCATION_TRADE_ID_INVALID")
    if _parse_replay_time(row["trade_created_at"]) is None:
        findings.append("EXEC_V4_ALLOCATION_TRADE_TIME_INVALID")
    if _finite_number(row["side_price"], minimum=0, maximum=1) is None:
        findings.append("EXEC_V4_ALLOCATION_SIDE_PRICE_INVALID")
    if _finite_number(row["queue_quantity"], minimum=0) is None:
        findings.append("EXEC_V4_ALLOCATION_QUEUE_QUANTITY_INVALID")
    if _finite_number(row["fill_quantity"], minimum=0) is None:
        findings.append("EXEC_V4_ALLOCATION_FILL_QUANTITY_INVALID")
    if _binary_flag(row["counterfactual"]) is None:
        findings.append("EXEC_V4_ALLOCATION_COUNTERFACTUAL_INVALID")
    return findings


def _tape_replay_input_findings(row: sqlite3.Row) -> list[str]:
    findings: list[str] = []
    if not str(row["trade_id"] or "").strip():
        findings.append("EXEC_V4_TAPE_TRADE_ID_INVALID")
    if _parse_replay_time(row["created_time"]) is None:
        findings.append("EXEC_V4_TAPE_TIME_INVALID")
    if _finite_number(row["count"], minimum=0) in {None, 0.0}:
        findings.append("EXEC_V4_TAPE_QUANTITY_INVALID")
    if _finite_number(row["yes_price"], minimum=0, maximum=1) is None:
        findings.append("EXEC_V4_TAPE_YES_PRICE_INVALID")
    if _binary_flag(row["is_block_trade"]) is None:
        findings.append("EXEC_V4_TAPE_BLOCK_FLAG_INVALID")
    return findings


def _normalized_tape_trade(row: sqlite3.Row) -> PublicAggressorTrade | None:
    if _tape_replay_input_findings(row):
        return None
    raw = _json_object(row["raw_json"])
    payload: dict[str, object] = {
        "trade_id": str(row["trade_id"] or ""),
        "created_time": row["created_time"],
        "count": row["count"],
        "yes_price": row["yes_price"],
        "is_block_trade": _binary_flag(row["is_block_trade"]),
        "taker_book_side": raw.get("taker_book_side")
        or row["taker_book_side"],
        "taker_outcome_side": raw.get("taker_outcome_side")
        or raw.get("taker_side"),
    }
    try:
        trade = normalize_public_trade(payload)
    except (ArithmeticError, TypeError, ValueError):
        return None
    if trade is None:
        return None
    persisted_maker_side = str(row["maker_side"] or "").upper()
    if persisted_maker_side and persisted_maker_side != trade.maker_side:
        return None
    return trade


def _replay_order(row: sqlite3.Row) -> RestingMakerOrder | None:
    if _order_replay_input_findings(row):
        return None
    placed_at = _parse_replay_time(row["created_at"])
    side = str(row["side"] or "").upper()
    limit_value = row["limit_price"]
    if limit_value is None:
        limit_value = row["entry_price"]
    requested = row["requested_contracts"]
    entry_bid = row["entry_bid"]
    entry_bid_size = row["entry_bid_size"]
    limit_price = _finite_number(limit_value, minimum=0, maximum=1)
    quantity = _finite_number(requested, minimum=0)
    visible_bid = (
        _finite_number(entry_bid, minimum=0, maximum=1)
        if entry_bid is not None
        else None
    )
    order_id = _positive_row_id(row["id"])
    if (
        order_id is None
        or placed_at is None
        or side not in {"YES", "NO"}
        or quantity in {None, 0.0}
        or limit_price is None
    ):
        return None
    # At or below the observed bid, displayed size is the immutable initial
    # queue. An improving bid has provable zero queue even if depth is absent.
    if entry_bid_size is None and (
        visible_bid is None or round(limit_price, 6) <= round(visible_bid, 6)
    ):
        return None
    try:
        queue_ahead = initial_queue_ahead(
            limit_price,
            visible_bid,
            _finite_number(entry_bid_size, minimum=0),
        )
    except (TypeError, ValueError):
        return None
    queue_price = visible_bid if visible_bid is not None else limit_price
    return RestingMakerOrder(
        order_id=order_id,
        side=side,  # type: ignore[arg-type]
        limit_price=Decimal(str(round(limit_price, 6))),
        quantity=Decimal(str(quantity)),
        queue_ahead=Decimal(str(queue_ahead)),
        placed_at=placed_at,
        queue_price=Decimal(str(round(queue_price, 6))),
    )


def _maker_replay_scope(row: sqlite3.Row, evidence: dict[str, Any]) -> bool:
    if row["parent_order_id"]:
        return False
    if str(row["entry_mode"] or "market") != "limit":
        return False
    if str(row["fill_model"] or "") == "immediate_visible_quote":
        return False
    return (
        str(row["execution_model_version"] or "") == EXECUTION_MODEL_VERSION
        or evidence.get("model") == "maker_allocator_price_time_v4"
    )


def _expected_consumptions(
    trades: list[PublicAggressorTrade],
    orders: list[RestingMakerOrder],
) -> dict[int, dict[str, dict[str, float]]]:
    return {
        order_id: allocation.consumption_by_trade()
        for order_id, allocation in allocate_maker_fills(trades, orders).items()
    }


def _consumption_mapping_matches(
    actual: object,
    expected: dict[str, dict[str, float]],
) -> bool:
    if not isinstance(actual, dict) or set(map(str, actual)) != set(expected):
        return False
    for trade_id, amounts in expected.items():
        item = actual.get(trade_id)
        if not isinstance(item, dict) or set(item) != {
            "queue_quantity",
            "fill_quantity",
            "total_quantity",
        }:
            return False
        if any(
            not _close_number(item.get(key), amounts[key])
            for key in ("queue_quantity", "fill_quantity", "total_quantity")
        ):
            return False
    return True


def _exec_v4_replay_findings(
    orders: list[sqlite3.Row],
    allocation_rows: list[sqlite3.Row],
    tape_rows: list[sqlite3.Row],
    maker_claim_rows: list[sqlite3.Row],
) -> dict[int, list[str]]:
    """Reproduce v4 allocation evidence through the production allocator."""

    evidences = {
        order_id: _json_object(row["fill_evidence_json"])
        for row in orders
        if (order_id := _positive_row_id(row["id"])) is not None
    }
    candidates = {
        order_id: row
        for row in orders
        if (order_id := _positive_row_id(row["id"])) is not None
        and evidences[order_id].get("model")
        == "maker_allocator_price_time_v4"
    }
    findings: dict[int, list[str]] = {order_id: [] for order_id in candidates}
    candidate_ids_by_ticker: dict[str, set[int]] = defaultdict(set)
    for order_id, row in candidates.items():
        candidate_ids_by_ticker[str(row["market_ticker"])].add(order_id)

    def attach_to_ticker(ticker: object, reasons: list[str]) -> None:
        for candidate_id in candidate_ids_by_ticker.get(str(ticker), set()):
            for reason in reasons:
                _append_finding(findings[candidate_id], reason)

    allocations_by_order: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for allocation in allocation_rows:
        allocation_findings = _allocation_replay_input_findings(allocation)
        allocation_order_id = _positive_row_id(allocation["order_id"])
        if allocation_findings:
            if allocation_order_id in candidates:
                for reason in allocation_findings:
                    _append_finding(findings[allocation_order_id], reason)
            else:
                attach_to_ticker(allocation["market_ticker"], allocation_findings)
        if allocation_order_id is not None:
            allocations_by_order[allocation_order_id].append(allocation)

    for tape_row in tape_rows:
        attach_to_ticker(
            tape_row["ticker"], _tape_replay_input_findings(tape_row)
        )

    for claim in maker_claim_rows:
        claim_findings: list[str] = []
        if _positive_row_id(claim["order_id"]) is None:
            claim_findings.append("EXEC_V4_PRIOR_CLAIM_ORDER_ID_INVALID")
        if not str(claim["trade_id"] or "").strip():
            claim_findings.append("EXEC_V4_PRIOR_CLAIM_TRADE_ID_INVALID")
        if _finite_number(claim["quantity"], minimum=0) is None:
            claim_findings.append("EXEC_V4_PRIOR_CLAIM_QUANTITY_INVALID")
        attach_to_ticker(claim["market_ticker"], claim_findings)

    for order_id, row in candidates.items():
        evidence = evidences[order_id]
        if str(row["execution_model_version"] or "") != EXECUTION_MODEL_VERSION:
            _append_finding(findings[order_id], "EXEC_V4_ORDER_VERSION_MISMATCH")
        if str(evidence.get("execution_model_version") or "") != EXECUTION_MODEL_VERSION:
            _append_finding(findings[order_id], "EXEC_V4_EVIDENCE_VERSION_MISMATCH")
        seen_trade_ids: set[str] = set()
        for allocation in allocations_by_order.get(order_id, []):
            trade_id = str(allocation["trade_id"])
            if trade_id in seen_trade_ids:
                _append_finding(findings[order_id], "EXEC_V4_ALLOCATION_DUPLICATE")
            seen_trade_ids.add(trade_id)
            if str(allocation["execution_model_version"] or "") != EXECUTION_MODEL_VERSION:
                _append_finding(
                    findings[order_id], "EXEC_V4_ALLOCATION_VERSION_MISMATCH"
                )

    scope_rows = [
        row
        for row in orders
        if (order_id := _positive_row_id(row["id"])) is not None
        and _maker_replay_scope(row, evidences[order_id])
    ]
    for row in scope_rows:
        attach_to_ticker(row["market_ticker"], _order_replay_input_findings(row))
    scope_by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in scope_rows:
        order_id = _positive_row_id(row["id"])
        if order_id is None:
            continue
        isolation = (
            f"research:{order_id}"
            if str(row["account_id"] or "") == RESEARCH_ACCOUNT_ID
            else "capital"
        )
        scope_by_key[(str(row["market_ticker"]), isolation)].append(row)

    tape_by_ticker: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for tape_row in tape_rows:
        tape_by_ticker[str(tape_row["ticker"])].append(tape_row)

    all_scope_ids = {
        order_id
        for row in scope_rows
        if (order_id := _positive_row_id(row["id"])) is not None
    }
    for (ticker, isolation), rows in scope_by_key.items():
        candidate_ids = {
            order_id
            for row in rows
            if (order_id := _positive_row_id(row["id"])) is not None
        } & set(candidates)
        if any(
            reason.endswith("_INVALID")
            for order_id in candidate_ids
            for reason in findings[order_id]
        ):
            for order_id in candidate_ids:
                _append_finding(findings[order_id], "INSUFFICIENT_REPLAY_EVIDENCE")
            continue
        replay_orders_by_id = {
            order_id: _replay_order(row)
            for row in rows
            if (order_id := _positive_row_id(row["id"])) is not None
        }
        if any(order is None for order in replay_orders_by_id.values()):
            for order_id in candidate_ids:
                _append_finding(findings[order_id], "INSUFFICIENT_REPLAY_EVIDENCE")
            continue

        normalized_trades: list[PublicAggressorTrade] = []
        tape_invalid = False
        for tape_row in tape_by_ticker.get(ticker, []):
            block_flag = _binary_flag(tape_row["is_block_trade"])
            if block_flag is None:
                tape_invalid = True
                continue
            if block_flag:
                continue
            trade = _normalized_tape_trade(tape_row)
            if trade is None:
                tape_invalid = True
                continue
            normalized_trades.append(trade)
        if tape_invalid or not normalized_trades:
            for order_id in candidate_ids:
                _append_finding(findings[order_id], "INSUFFICIENT_REPLAY_EVIDENCE")
            if tape_invalid:
                continue

        if isolation == "capital":
            external_claims: dict[str, float] = defaultdict(float)
            for claim in maker_claim_rows:
                claim_order_id = _positive_row_id(claim["order_id"])
                claim_quantity = _finite_number(claim["quantity"], minimum=0)
                if (
                    str(claim["market_ticker"]) == ticker
                    and claim_order_id is not None
                    and claim_order_id not in all_scope_ids
                    and claim_quantity is not None
                ):
                    external_claims[str(claim["trade_id"])] += claim_quantity
            for allocation in allocation_rows:
                allocation_order_id = _positive_row_id(allocation["order_id"])
                counterfactual = _binary_flag(allocation["counterfactual"])
                queue_quantity = _finite_number(
                    allocation["queue_quantity"], minimum=0
                )
                fill_quantity = _finite_number(
                    allocation["fill_quantity"], minimum=0
                )
                if (
                    str(allocation["market_ticker"]) == ticker
                    and counterfactual is False
                    and allocation_order_id is not None
                    and allocation_order_id not in all_scope_ids
                    and queue_quantity is not None
                    and fill_quantity is not None
                ):
                    external_claims[str(allocation["trade_id"])] += (
                        queue_quantity + fill_quantity
                    )
            normalized_trades = apply_volume_claims(
                normalized_trades, dict(external_claims)
            )

        expected_by_order = _expected_consumptions(
            normalized_trades,
            [
                order
                for order in replay_orders_by_id.values()
                if order is not None
            ],
        )
        trade_by_id = {trade.trade_id: trade for trade in normalized_trades}
        for order_id in candidate_ids:
            row = candidates[order_id]
            evidence = evidences[order_id]
            expected = expected_by_order.get(order_id, {})
            actual_rows = allocations_by_order.get(order_id, [])
            actual_by_trade: dict[str, sqlite3.Row] = {}
            for allocation in actual_rows:
                actual_by_trade.setdefault(str(allocation["trade_id"]), allocation)
            missing = set(expected) - set(actual_by_trade)
            filled_contracts = _finite_number(row["filled_contracts"], minimum=0)
            if missing or (
                not actual_rows
                and filled_contracts is not None
                and filled_contracts > 0
            ):
                _append_finding(findings[order_id], "EXEC_V4_ALLOCATION_MISSING")
            if set(actual_by_trade) - set(expected):
                _append_finding(
                    findings[order_id], "EXEC_V4_ALLOCATION_REPLAY_MISMATCH"
                )
            expected_counterfactual = isolation != "capital"
            for trade_id in set(expected) & set(actual_by_trade):
                allocation = actual_by_trade[trade_id]
                trade = trade_by_id.get(trade_id)
                amounts = expected[trade_id]
                allocation_evidence = _json_object(allocation["evidence_json"])
                if (
                    trade is None
                    or str(allocation["market_ticker"]) != ticker
                    or _parse_replay_time(allocation["trade_created_at"])
                    != trade.created_at
                    or str(allocation["maker_side"] or "").upper()
                    != trade.maker_side
                    or not _close_number(
                        allocation["side_price"],
                        trade.side_price(str(row["side"] or "YES")),
                    )
                    or not _close_number(
                        allocation["queue_quantity"], amounts["queue_quantity"]
                    )
                    or not _close_number(
                        allocation["fill_quantity"], amounts["fill_quantity"]
                    )
                    or _binary_flag(allocation["counterfactual"])
                    != expected_counterfactual
                    or not _consumption_mapping_matches(
                        {trade_id: allocation_evidence}, {trade_id: amounts}
                    )
                ):
                    _append_finding(
                        findings[order_id], "EXEC_V4_ALLOCATION_REPLAY_MISMATCH"
                    )

            expected_fills = {
                trade_id: amounts["fill_quantity"]
                for trade_id, amounts in expected.items()
                if amounts["fill_quantity"] > _REPLAY_TOLERANCE
            }
            actual_fills = evidence.get("allocations")
            fills_match = isinstance(actual_fills, dict) and set(
                map(str, actual_fills)
            ) == set(expected_fills) and all(
                _close_number(actual_fills.get(trade_id), quantity)
                for trade_id, quantity in expected_fills.items()
            )
            expected_fill_total = sum(expected_fills.values())
            expected_queue_total = sum(
                amounts["queue_quantity"] for amounts in expected.values()
            )
            expected_trade_ids = sorted(expected)
            replay_order = replay_orders_by_id[order_id]
            assert replay_order is not None
            requested_quantity = _finite_number(
                row["requested_contracts"], minimum=0
            )
            queue_ahead = _finite_number(replay_order.queue_ahead, minimum=0)
            if requested_quantity is None or queue_ahead is None:
                _append_finding(
                    findings[order_id], "INSUFFICIENT_REPLAY_EVIDENCE"
                )
                continue
            if (
                not _consumption_mapping_matches(evidence.get("consumptions"), expected)
                or not fills_match
                or evidence.get("trade_ids") != expected_trade_ids
                or not _close_number(
                    evidence.get("requested_quantity"), row["requested_contracts"]
                )
                or not _close_number(
                    evidence.get("filled_quantity"), expected_fill_total
                )
                or not _close_number(
                    evidence.get("remaining_quantity"),
                    requested_quantity - expected_fill_total,
                )
                or not _close_number(
                    evidence.get("queue_remaining"),
                    queue_ahead - expected_queue_total,
                )
                or bool(evidence.get("counterfactual")) != expected_counterfactual
                or bool(evidence.get("research_shadow")) != expected_counterfactual
            ):
                _append_finding(
                    findings[order_id], "EXEC_V4_EVIDENCE_REPLAY_MISMATCH"
                )

    return findings


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_value(row: object, key: str, default: object = None) -> object:
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return default


def _order_lifecycle_findings(row: sqlite3.Row) -> list[str]:
    """Validate persisted lifecycle fields before classification arithmetic."""

    findings: list[str] = []
    if _positive_row_id(_row_value(row, "id")) is None:
        findings.append("ORDER_ID_INVALID")
    if _parse_replay_time(_row_value(row, "created_at")) is None:
        findings.append("ORDER_PLACED_AT_INVALID")
    contracts = _finite_number(_row_value(row, "contracts"), minimum=0)
    if contracts is None or contracts <= 0:
        findings.append("ORDER_CONTRACTS_INVALID")
    _, parent_invalid = _optional_parent_id(_row_value(row, "parent_order_id"))
    if parent_invalid:
        findings.append("PARENT_ORDER_ID_INVALID")

    status = str(_row_value(row, "status") or "")
    if status in {"PAPER_SETTLED", "PAPER_CLOSED"} and _finite_number(
        _row_value(row, "realized_pnl")
    ) is None:
        findings.append("REALIZED_PNL_INVALID")
    if status == "PAPER_SETTLED":
        if _parse_replay_time(_row_value(row, "settled_at")) is None:
            findings.append("SETTLED_AT_INVALID")
        if _finite_number(_row_value(row, "settlement_high_f")) is None:
            findings.append("SETTLEMENT_HIGH_INVALID")
        if _binary_flag(_row_value(row, "resolved_yes")) is None:
            findings.append("RESOLVED_YES_INVALID")
    elif status == "PAPER_CLOSED":
        if _parse_replay_time(_row_value(row, "closed_at")) is None:
            findings.append("CLOSED_AT_INVALID")
        if _finite_number(
            _row_value(row, "exit_price"), minimum=0, maximum=1
        ) is None:
            findings.append("EXIT_PRICE_INVALID")
        if _finite_number(
            _row_value(row, "exit_fee_per_contract"), minimum=0
        ) is None:
            findings.append("EXIT_FEE_INVALID")
    return findings


def _entry_findings(
    row: sqlite3.Row,
    evidence: dict[str, Any],
    duplicate_trade_ids: set[str],
    current_findings: list[str] | None = None,
) -> list[str]:
    status = str(row["status"])
    entry_mode = str(row["entry_mode"] or "market")
    fill_model = str(row["fill_model"] or "")
    if status in ("PAPER_LIMIT_RESTING", "PAPER_EXPIRED", "PAPER_CANCELLED"):
        return []  # never filled -> no entry evidence needed
    if entry_mode != "limit" or fill_model == "immediate_visible_quote":
        if str(row["created_at"] or "") < TAKER_SIZING_FIX_DATE:
            return ["TAKER_PRE_SIZING_FIX"]
        return []
    model = str(evidence.get("model") or "")
    if model == "maker_allocator_price_time_v4":
        return list(current_findings or [])
    if model == "maker_allocator_price_time_v3":
        return ["EXEC_V3_HISTORICAL_SEMANTICS"]
    if model == "maker_allocator_price_time_v2":
        return ["EXEC_V2_QUEUE_STATE_UNREPLAYABLE"]
    findings: list[str] = []
    side = str(row["side"] or "YES").upper()
    if side == "YES":
        findings.append("DIRECTION_INVALID")
    findings.append("LEGACY_TAPE_UNREPLAYABLE")
    trade_ids = evidence.get("trade_ids") or []
    if any(str(trade_id) in duplicate_trade_ids for trade_id in trade_ids):
        findings.append("DOUBLE_CREDITED")
    return findings


def _exit_findings(row: sqlite3.Row, outcome: dict[str, Any]) -> list[str]:
    status = str(row["status"])
    if status == "PAPER_SETTLED":
        return []  # settlement verified separately against final truth
    if status != "PAPER_CLOSED":
        return []
    if "exit_execution" not in outcome:
        return ["EXIT_DEPTH_UNVERIFIED"]
    execution = outcome.get("exit_execution")
    if not isinstance(execution, dict):
        return ["EXIT_EXECUTION_INVALID"]
    if "executed_quantity" not in execution:
        return ["EXIT_DEPTH_UNVERIFIED"]
    executed = _finite_number(execution.get("executed_quantity"), minimum=0)
    if executed is None or executed <= 0:
        return ["EXIT_EXECUTED_QUANTITY_INVALID"]
    depth_field = (
        "displayed_depth"
        if "displayed_depth" in execution
        else "displayed_bid_size"
        if "displayed_bid_size" in execution
        else None
    )
    if depth_field is None:
        return ["EXIT_DEPTH_UNVERIFIED"]
    displayed_depth = _finite_number(execution.get(depth_field), minimum=0)
    if displayed_depth is None or displayed_depth <= 0:
        return ["EXIT_DISPLAYED_DEPTH_INVALID"]
    if displayed_depth + 1e-9 < executed:
        return ["EXIT_DEPTH_INSUFFICIENT"]
    closed_at = _row_value(row, "closed_at")
    if _parse_replay_time(closed_at) is None:
        return ["CLOSED_AT_INVALID"]
    observed_at = execution.get("observed_at")
    if "observed_at" in execution and _parse_replay_time(observed_at) is None:
        return ["EXIT_OBSERVED_AT_INVALID"]
    if not depth_observation_is_contemporaneous(
        observed_at, closed_at
    ):
        return ["EXIT_DEPTH_STALE"]
    if (
        not execution.get("source")
        or not execution.get("observed_at")
        or execution.get("verification_status") != VERIFIED
    ):
        return ["EXIT_DEPTH_UNVERIFIED"]
    return []


def restate(db_path: Path) -> dict[str, Any]:
    """Build the immutable-evidence restatement report for one database."""

    with _connect_readonly(Path(db_path)) as conn:
        orders = conn.execute(
            "SELECT * FROM paper_orders WHERE status != 'REJECTED' ORDER BY created_at, id"
        ).fetchall()
        settlement_rows = conn.execute(
            "SELECT * FROM paper_settlement_verifications"
        ).fetchall()
        has_allocator_evidence = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='paper_maker_allocations'"
        ).fetchone() is not None
        allocation_rows = (
            conn.execute("SELECT * FROM paper_maker_allocations").fetchall()
            if has_allocator_evidence
            else []
        )
        has_maker_claims = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='maker_volume_claims'"
        ).fetchone() is not None
        maker_claim_rows = (
            conn.execute("SELECT * FROM maker_volume_claims").fetchall()
            if has_maker_claims
            else []
        )
        has_tape = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='dataset_kalshi_trades'"
        ).fetchone() is not None
        tape_rows = (
            conn.execute("SELECT * FROM dataset_kalshi_trades").fetchall()
            if has_tape
            else []
        )

    settlement_checks: dict[int, str] = {}
    invalid_settlement_tickers: set[str | None] = set()
    for check_row in settlement_rows:
        check_order_id = _positive_row_id(_row_value(check_row, "order_id"))
        if check_order_id is None:
            check_ticker = str(_row_value(check_row, "market_ticker") or "").strip()
            invalid_settlement_tickers.add(check_ticker or None)
            continue
        settlement_checks[check_order_id] = str(
            _row_value(check_row, "verification_status") or ""
        )

    allocations_by_order: dict[int, list[sqlite3.Row]] = defaultdict(list)
    capital_consumption: dict[str, float] = defaultdict(float)
    for allocation_row in allocation_rows:
        allocation_order_id = _positive_row_id(allocation_row["order_id"])
        if allocation_order_id is not None:
            allocations_by_order[allocation_order_id].append(allocation_row)
        counterfactual = _binary_flag(allocation_row["counterfactual"])
        queue_quantity = _finite_number(
            allocation_row["queue_quantity"], minimum=0
        )
        fill_quantity = _finite_number(allocation_row["fill_quantity"], minimum=0)
        if (
            counterfactual is False
            and queue_quantity is not None
            and fill_quantity is not None
        ):
            capital_consumption[str(allocation_row["trade_id"])] += (
                queue_quantity + fill_quantity
            )
    tape_by_id = {str(tape_row["trade_id"]): tape_row for tape_row in tape_rows}
    overclaimed_trade_ids: set[str] = set()
    for trade_id, consumed in capital_consumption.items():
        tape = tape_by_id.get(trade_id)
        tape_quantity = (
            _finite_number(tape["count"], minimum=0) if tape is not None else None
        )
        if tape is None or (
            tape_quantity is not None and consumed > tape_quantity + 1e-9
        ):
            overclaimed_trade_ids.add(trade_id)

    current_findings_by_order: dict[int, list[str]] = {}
    for row in orders:
        order_id = _positive_row_id(row["id"])
        if order_id is None:
            continue
        evidence = _json_object(row["fill_evidence_json"])
        if evidence.get("model") != "maker_allocator_price_time_v4":
            continue
        findings: list[str] = []
        order_allocations = allocations_by_order.get(order_id, [])
        if not order_allocations:
            findings.append("EXEC_V4_ALLOCATION_MISSING")
        expected_fill = _finite_number(
            row["filled_contracts"]
            if "filled_contracts" in row.keys() and row["filled_contracts"] is not None
            else row["contracts"],
            minimum=0,
        )
        recorded_fills = [
            _finite_number(item["fill_quantity"], minimum=0)
            for item in order_allocations
        ]
        if (
            expected_fill is not None
            and all(item is not None for item in recorded_fills)
            and abs(
                sum(item for item in recorded_fills if item is not None)
                - expected_fill
            )
            > 1e-9
        ):
            findings.append("EXEC_V4_FILL_QUANTITY_MISMATCH")
        side = str(row["side"] or "YES").upper()
        limit_price = _finite_number(
            row["limit_price"]
            if row["limit_price"] is not None
            else row["entry_price"],
            minimum=0,
            maximum=1,
        )
        queue_price = _finite_number(
            row["entry_bid"] if row["entry_bid"] is not None else limit_price,
            minimum=0,
            maximum=1,
        )
        created_at = _parse_replay_time(row["created_at"])
        for allocation_row in order_allocations:
            trade_id = str(allocation_row["trade_id"])
            tape = tape_by_id.get(trade_id)
            if tape is None:
                findings.append("EXEC_V4_TAPE_MISSING")
                continue
            if str(allocation_row["maker_side"]).upper() != side:
                findings.append("EXEC_V4_DIRECTION_INVALID")
            trade_created_at = _parse_replay_time(allocation_row["trade_created_at"])
            if (
                created_at is not None
                and trade_created_at is not None
                and trade_created_at <= created_at
            ):
                findings.append("EXEC_V4_TRADE_NOT_LATER")
            side_price = _finite_number(
                allocation_row["side_price"], minimum=0, maximum=1
            )
            queue_quantity = _finite_number(
                allocation_row["queue_quantity"], minimum=0
            )
            fill_quantity = _finite_number(
                allocation_row["fill_quantity"], minimum=0
            )
            if (
                limit_price is not None
                and queue_price is not None
                and side_price is not None
                and queue_quantity is not None
                and fill_quantity is not None
                and (
                    (fill_quantity > 0 and side_price > limit_price + 1e-9)
                    or (
                        queue_quantity > 0
                        and side_price > queue_price + 1e-9
                    )
                )
            ):
                findings.append("EXEC_V4_PRICE_INVALID")
            if trade_id in overclaimed_trade_ids:
                findings.append("EXEC_V4_VOLUME_OVERCLAIMED")
        current_findings_by_order[order_id] = list(dict.fromkeys(findings))

    replay_findings = _exec_v4_replay_findings(
        orders, allocation_rows, tape_rows, maker_claim_rows
    )
    for order_id, findings in replay_findings.items():
        combined = current_findings_by_order.setdefault(order_id, [])
        for finding in findings:
            _append_finding(combined, finding)

    # A public trade id credited by more than one capital-consuming order's
    # evidence is the double-credit signature (production 226/227, 261/262).
    trade_id_owners: dict[str, set[int]] = defaultdict(set)
    evidences: dict[int, dict[str, Any]] = {}
    for row in orders:
        order_id = _positive_row_id(row["id"])
        if order_id is None:
            continue
        evidence = _json_object(row["fill_evidence_json"])
        evidences[order_id] = evidence
        if evidence.get("research_shadow") or evidence.get("counterfactual"):
            continue
        for trade_id in evidence.get("trade_ids") or []:
            trade_id_owners[str(trade_id)].add(order_id)
    duplicate_trade_ids = {
        trade_id for trade_id, owners in trade_id_owners.items() if len(owners) > 1
    }

    classes: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"orders": 0, "realized_pnl": 0.0}
    )
    per_profile: dict[str, dict[str, Any]] = defaultdict(
        lambda: defaultdict(lambda: {"orders": 0, "realized_pnl": 0.0})
    )
    resolved_statuses = ("PAPER_SETTLED", "PAPER_CLOSED")
    for row in orders:
        order_id = _positive_row_id(row["id"])
        own_evidence = _json_object(row["fill_evidence_json"])
        evidence = evidences.get(order_id, own_evidence)
        findings = _order_lifecycle_findings(row)
        # A partial-close lot carries no fill evidence of its own -- its entry
        # was executed (and verified) on the parent order it split from.
        parent_id, _ = _optional_parent_id(
            _row_value(row, "parent_order_id")
        )
        if parent_id is not None:
            parent_evidence = evidences.get(parent_id)
            if parent_evidence is None:
                _append_finding(findings, "PARENT_ORDER_MISSING")
                evidence = {}
            else:
                evidence = parent_evidence
        entry_evidence_order_id = parent_id if parent_id is not None else order_id
        outcome = _json_object(row["outcome_diagnostics_json"])
        findings += _entry_findings(
            row,
            evidence,
            duplicate_trade_ids,
            current_findings_by_order.get(entry_evidence_order_id),
        )
        if (
            evidence.get("model") == "maker_allocator_price_time_v4"
            and str(row["execution_model_version"] or "")
            != EXECUTION_MODEL_VERSION
        ):
            _append_finding(findings, "EXEC_V4_ORDER_VERSION_MISMATCH")
        findings += _exit_findings(row, outcome)
        settlement_check = (
            settlement_checks.get(order_id) if order_id is not None else None
        )
        if settlement_check == "MISMATCH":
            findings.append("SETTLEMENT_MISMATCH")
        ticker = str(row["market_ticker"])
        if evidence.get("model") == "maker_allocator_price_time_v4" and (
            None in invalid_settlement_tickers
            or ticker in invalid_settlement_tickers
        ):
            findings.append("SETTLEMENT_VERIFICATION_ORDER_ID_INVALID")
        findings = list(dict.fromkeys(findings))
        verification = UNVERIFIABLE if findings else VERIFIED
        realized = _finite_number(row["realized_pnl"])
        profile = str(row["risk_profile"] or "live")
        status = str(row["status"])
        classes.append(
            {
                "order_id": order_id if order_id is not None else row["id"],
                "market_ticker": ticker,
                "target_date": str(row["target_date"]),
                "status": status,
                "risk_profile": profile,
                "execution_model_version": str(
                    row["execution_model_version"] or ""
                ),
                "fill_evidence_model": str(evidence.get("model") or ""),
                "verification": verification,
                "findings": findings,
                "realized_pnl": round(realized, 4)
                if status in resolved_statuses and realized is not None
                else None,
            }
        )
        if status in resolved_statuses:
            totals[verification]["orders"] += 1
            per_profile[profile][verification]["orders"] += 1
            if realized is not None:
                totals[verification]["realized_pnl"] += realized
                per_profile[profile][verification]["realized_pnl"] += realized

    legacy_realized = sum(
        bucket["realized_pnl"] for bucket in totals.values()
    )
    finding_counts: dict[str, int] = defaultdict(int)
    for entry in classes:
        for finding in entry["findings"]:
            finding_counts[finding] += 1

    return {
        "kind": "execution_restatement",
        "execution_model_version": EXECUTION_MODEL_VERSION,
        "accounting_policy_version": ACCOUNTING_POLICY_VERSION,
        "db_path": str(db_path),
        "orders_examined": len(orders),
        "legacy_view": {
            "note": "as originally booked; immutable and preserved",
            "resolved_orders": sum(b["orders"] for b in totals.values()),
            "realized_pnl": round(legacy_realized, 4),
        },
        "corrected_view": {
            "verified": {
                "orders": totals[VERIFIED]["orders"],
                "realized_pnl": round(totals[VERIFIED]["realized_pnl"], 4),
            },
            "unverifiable": {
                "orders": totals[UNVERIFIABLE]["orders"],
                "realized_pnl": round(totals[UNVERIFIABLE]["realized_pnl"], 4),
                "note": (
                    "insufficient or invalid execution evidence under "
                    f"{EXECUTION_MODEL_VERSION}; NOT trusted and NOT zeroed"
                ),
            },
        },
        "per_profile": {
            profile: {
                verification: {
                    "orders": bucket["orders"],
                    "realized_pnl": round(bucket["realized_pnl"], 4),
                }
                for verification, bucket in buckets.items()
            }
            for profile, buckets in per_profile.items()
        },
        "finding_counts": dict(sorted(finding_counts.items())),
        "duplicate_trade_ids": sorted(duplicate_trade_ids),
        "promotion_clock": {
            "rule": (
                "the 30-independent-day promotion clock restarts at the first "
                "trading day executed fully under the corrected execution "
                "model; no historical day qualifies"
            ),
            "boundary_execution_model_version": EXECUTION_MODEL_VERSION,
        },
        "orders": classes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only execution restatement (audit 2026-07-13 Batch D)"
    )
    parser.add_argument("db_path", type=Path, help="paper trading database (opened read-only)")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument(
        "--full", action="store_true", help="include the per-order classification list"
    )
    args = parser.parse_args(argv)

    report = restate(args.db_path)
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True))
    if not args.full:
        report = {key: value for key, value in report.items() if key != "orders"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
