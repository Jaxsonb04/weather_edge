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
from .fees import quadratic_fee_average_per_contract
from .logical_positions import LogicalPaperPosition, group_logical_positions
from .maker_fills import (
    EXECUTION_MODEL_VERSION,
    PublicAggressorTrade,
    RestingMakerOrder,
    allocate_maker_fills,
    apply_volume_claims,
    depth_observation_is_contemporaneous,
    normalize_public_trade,
    uses_current_maker_semantics,
)
from .paper_pnl import closed_position_pnl, settled_position_pnl
from .settlement_truth import integer_settlement_high_f, row_resolves_yes

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


def _strict_text_identity(value: object) -> str | None:
    """Accept only non-empty strings with no hidden surrounding whitespace."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


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
    active_until, active_finding = _order_active_until(row)
    if active_finding is not None:
        findings.append(active_finding)
    placed_at = _parse_replay_time(row["created_at"])
    if (
        active_until is not None
        and placed_at is not None
        and active_until <= placed_at
    ):
        findings.append("EXEC_V4_ORDER_ACTIVE_UNTIL_NOT_LATER")
    if str(_row_value(row, "status") or "") in {
        "PAPER_EXPIRED",
        "PAPER_PARTIAL_EXPIRED",
    }:
        expires_at = _parse_replay_time(_row_value(row, "expires_at"))
        if (
            expires_at is not None
            and placed_at is not None
            and expires_at <= placed_at
        ):
            findings.append("EXEC_V4_ORDER_EXPIRES_AT_NOT_LATER")
    return findings


def _order_active_until(
    row: sqlite3.Row,
) -> tuple[datetime | None, str | None]:
    """Return the earliest authoritative inclusive quote-terminal instant."""

    status = str(_row_value(row, "status") or "")
    cancelled_at = _row_value(row, "cancelled_at")
    if status == "PAPER_CLOSED":
        closed_at = _parse_replay_time(_row_value(row, "closed_at"))
        if closed_at is None:
            return None, "EXEC_V4_ORDER_CLOSED_AT_INVALID"
        if cancelled_at is None:
            return closed_at, None
        cancelled = _parse_replay_time(cancelled_at)
        if cancelled is None:
            return None, "EXEC_V4_ORDER_CANCELLED_AT_INVALID"
        return min(cancelled, closed_at), None
    if cancelled_at is not None or status in {
        "PAPER_CANCELLED",
        "PAPER_EXPIRED",
        "PAPER_PARTIAL_EXPIRED",
    }:
        parsed = _parse_replay_time(cancelled_at)
        if parsed is None:
            return None, "EXEC_V4_ORDER_CANCELLED_AT_INVALID"
        if status in {"PAPER_EXPIRED", "PAPER_PARTIAL_EXPIRED"}:
            expires_at = _parse_replay_time(_row_value(row, "expires_at"))
            if expires_at is None:
                return None, "EXEC_V4_ORDER_EXPIRES_AT_INVALID"
        return parsed, None
    return None, None


def _allocation_replay_input_findings(row: sqlite3.Row) -> list[str]:
    findings: list[str] = []
    if _positive_row_id(row["order_id"]) is None:
        findings.append("EXEC_V4_ALLOCATION_ORDER_ID_INVALID")
    if _strict_text_identity(row["trade_id"]) is None:
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
    if _strict_text_identity(row["trade_id"]) is None:
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
    active_until, active_finding = _order_active_until(row)
    if active_finding is not None:
        return None
    return RestingMakerOrder(
        order_id=order_id,
        side=side,  # type: ignore[arg-type]
        limit_price=Decimal(str(round(limit_price, 6))),
        quantity=Decimal(str(quantity)),
        queue_ahead=Decimal(str(queue_ahead)),
        placed_at=placed_at,
        queue_price=Decimal(str(round(queue_price, 6))),
        active_until=active_until,
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
    *,
    known_order_ids: set[int] | None = None,
    known_order_tickers: dict[int, str] | None = None,
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
    persisted_order_ids = known_order_ids or set(evidences)
    persisted_order_tickers = known_order_tickers or {
        order_id: str(row["market_ticker"])
        for row in orders
        if (order_id := _positive_row_id(row["id"])) is not None
    }
    known_tape_ids = {
        trade_id
        for tape_row in tape_rows
        if (trade_id := _strict_text_identity(tape_row["trade_id"])) is not None
    }
    trade_owner_ids: dict[str, set[int]] = defaultdict(set)
    ambiguous_trade_ownership: set[str] = set()
    for shared_row in [*allocation_rows, *maker_claim_rows]:
        shared_trade_id = _strict_text_identity(shared_row["trade_id"])
        if shared_trade_id is None:
            continue
        shared_order_id = _positive_row_id(shared_row["order_id"])
        if shared_order_id is None or shared_order_id not in persisted_order_ids:
            ambiguous_trade_ownership.add(shared_trade_id)
        else:
            trade_owner_ids[shared_trade_id].add(shared_order_id)

    def attach_to_ticker(
        ticker: object,
        reasons: list[str],
        *,
        identity_prefix: str | None = None,
        provably_unrelated: bool = False,
    ) -> None:
        attached_reasons = list(reasons)
        normalized_ticker = _strict_text_identity(ticker)
        if identity_prefix is None:
            target_ids = candidate_ids_by_ticker.get(str(ticker), set())
        elif normalized_ticker is None:
            target_ids = set(candidates)
            attached_reasons.append(f"{identity_prefix}_INVALID")
        elif normalized_ticker not in candidate_ids_by_ticker:
            if provably_unrelated:
                return
            target_ids = set(candidates)
            attached_reasons.append(f"{identity_prefix}_UNRESOLVABLE")
        else:
            target_ids = candidate_ids_by_ticker[normalized_ticker]
        for candidate_id in target_ids:
            for reason in attached_reasons:
                _append_finding(findings[candidate_id], reason)

    allocations_by_order: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for allocation in allocation_rows:
        allocation_findings = _allocation_replay_input_findings(allocation)
        allocation_order_id = _positive_row_id(allocation["order_id"])
        allocation_trade_id = _strict_text_identity(allocation["trade_id"])
        if (
            allocation_order_id is not None
            and allocation_order_id not in persisted_order_ids
        ):
            allocation_findings.append(
                "EXEC_V4_ALLOCATION_ORDER_ID_UNRESOLVABLE"
            )
        if (
            allocation_trade_id is not None
            and allocation_trade_id not in known_tape_ids
        ):
            allocation_findings.append(
                "EXEC_V4_ALLOCATION_TRADE_ID_UNRESOLVABLE"
            )
        attach_to_ticker(
            allocation["market_ticker"],
            allocation_findings,
            identity_prefix="EXEC_V4_ALLOCATION_TICKER",
            provably_unrelated=(
                allocation_order_id is not None
                and allocation_order_id in persisted_order_ids
                and allocation_order_id not in candidates
                and _strict_text_identity(allocation["market_ticker"])
                == persisted_order_tickers.get(allocation_order_id)
            ),
        )
        if allocation_order_id is not None:
            allocations_by_order[allocation_order_id].append(allocation)

    for tape_row in tape_rows:
        tape_trade_id = _strict_text_identity(tape_row["trade_id"])
        tape_owners = trade_owner_ids.get(tape_trade_id or "", set())
        attach_to_ticker(
            tape_row["ticker"],
            _tape_replay_input_findings(tape_row),
            identity_prefix="EXEC_V4_TAPE_TICKER",
            provably_unrelated=(
                bool(tape_owners)
                and tape_trade_id not in ambiguous_trade_ownership
                and all(owner_id not in candidates for owner_id in tape_owners)
                and all(
                    _strict_text_identity(tape_row["ticker"])
                    == persisted_order_tickers.get(owner_id)
                    for owner_id in tape_owners
                )
            ),
        )

    for claim in maker_claim_rows:
        claim_findings: list[str] = []
        claim_order_id = _positive_row_id(claim["order_id"])
        claim_trade_id = _strict_text_identity(claim["trade_id"])
        if claim_order_id is None:
            claim_findings.append("EXEC_V4_PRIOR_CLAIM_ORDER_ID_INVALID")
        elif claim_order_id not in persisted_order_ids:
            claim_findings.append(
                "EXEC_V4_PRIOR_CLAIM_ORDER_ID_UNRESOLVABLE"
            )
        if claim_trade_id is None:
            claim_findings.append("EXEC_V4_PRIOR_CLAIM_TRADE_ID_INVALID")
        elif claim_trade_id not in known_tape_ids:
            claim_findings.append(
                "EXEC_V4_PRIOR_CLAIM_TRADE_ID_UNRESOLVABLE"
            )
        if _finite_number(claim["quantity"], minimum=0) is None:
            claim_findings.append("EXEC_V4_PRIOR_CLAIM_QUANTITY_INVALID")
        attach_to_ticker(
            claim["market_ticker"],
            claim_findings,
            identity_prefix="EXEC_V4_PRIOR_CLAIM_TICKER",
            provably_unrelated=(
                claim_order_id is not None
                and claim_order_id in persisted_order_ids
                and claim_order_id not in candidates
                and _strict_text_identity(claim["market_ticker"])
                == persisted_order_tickers.get(claim_order_id)
            ),
        )

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
            reason.endswith(("_INVALID", "_UNRESOLVABLE"))
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


def _outcome_accounting_matches(
    outcome: dict[str, Any],
    *,
    event: str,
    resolved_at: datetime,
    settlement_high_f: float | None,
    resolved_yes: bool | None,
    position_won: bool | None,
    realized_pnl: float,
    contracts: float,
) -> bool:
    recorded = outcome.get("outcome")
    if not isinstance(recorded, dict) or recorded.get("event") != event:
        return False
    recorded_at = _parse_replay_time(recorded.get("resolved_at"))
    if recorded_at != resolved_at:
        return False
    if settlement_high_f is not None and not _close_number(
        recorded.get("settlement_high_f"), settlement_high_f
    ):
        return False
    if recorded.get("resolved_yes") is not resolved_yes:
        return False
    if recorded.get("position_won") is not position_won:
        return False
    recorded_pnl = _finite_number(recorded.get("realized_pnl"))
    recorded_per_contract = _finite_number(recorded.get("pnl_per_contract"))
    return (
        recorded_pnl is not None
        and abs(recorded_pnl - realized_pnl) <= 1e-6
        and recorded_per_contract is not None
        and abs(recorded_per_contract - realized_pnl / contracts) <= 1e-6
    )


def _settled_accounting_findings(
    row: sqlite3.Row,
    outcome: dict[str, Any],
    check: sqlite3.Row | None,
) -> list[str]:
    findings: list[str] = []
    if check is None or str(_row_value(check, "verification_status") or "") != "MATCH":
        if check is not None and str(
            _row_value(check, "verification_status") or ""
        ) == "MISMATCH":
            findings.append("SETTLEMENT_MISMATCH")
        else:
            findings.append("SETTLEMENT_VERIFICATION_REQUIRED")
        return findings

    checked_at = _parse_replay_time(_row_value(check, "checked_at"))
    settled_at = _parse_replay_time(_row_value(row, "settled_at"))
    if checked_at is None or settled_at is None or checked_at < settled_at:
        findings.append("SETTLEMENT_VERIFICATION_CHECKED_AT_INVALID")
    booked_high = _finite_number(
        _row_value(check, "booked_high_f"), minimum=-100, maximum=200
    )
    final_high = _finite_number(
        _row_value(check, "final_high_f"), minimum=-100, maximum=200
    )
    order_high = _finite_number(
        _row_value(row, "settlement_high_f"), minimum=-100, maximum=200
    )
    if final_high is None:
        findings.append("SETTLEMENT_VERIFICATION_FINAL_HIGH_INVALID")
    elif not _close_number(final_high, integer_settlement_high_f(final_high)):
        findings.append("SETTLEMENT_VERIFICATION_FINAL_HIGH_INVALID")
    if booked_high is None:
        findings.append("SETTLEMENT_VERIFICATION_BOOKED_HIGH_INVALID")
    elif not _close_number(booked_high, integer_settlement_high_f(booked_high)):
        findings.append("SETTLEMENT_VERIFICATION_BOOKED_HIGH_INVALID")
    if order_high is None or not _close_number(
        order_high, integer_settlement_high_f(order_high)
    ):
        findings.append("SETTLEMENT_HIGH_INVALID")
    if (
        booked_high is not None
        and order_high is not None
        and not _close_number(booked_high, order_high)
    ):
        findings.append("SETTLEMENT_VERIFICATION_BOOKED_HIGH_MISMATCH")
    if (
        final_high is not None
        and order_high is not None
        and not _close_number(final_high, order_high)
    ):
        findings.append("SETTLEMENT_VERIFICATION_FINAL_HIGH_MISMATCH")
    if findings or final_high is None or settled_at is None:
        return findings

    canonical_high = integer_settlement_high_f(final_high)
    canonical_resolved_yes = row_resolves_yes(row, canonical_high)
    recorded_resolved_yes = _binary_flag(_row_value(row, "resolved_yes"))
    side = str(_row_value(row, "side") or "").upper()
    if recorded_resolved_yes is not canonical_resolved_yes or side not in {
        "YES",
        "NO",
    }:
        findings.append("SETTLEMENT_OUTCOME_MISMATCH")
        return findings
    position_won = (
        canonical_resolved_yes if side == "YES" else not canonical_resolved_yes
    )
    contracts = _finite_number(_row_value(row, "contracts"), minimum=0)
    cost = _finite_number(
        _row_value(row, "cost_per_contract"), minimum=0, maximum=1
    )
    recorded_pnl = _finite_number(_row_value(row, "realized_pnl"))
    if contracts in {None, 0.0} or cost is None or recorded_pnl is None:
        return findings
    expected_pnl = settled_position_pnl(contracts, cost, position_won)
    if not _close_number(recorded_pnl, expected_pnl):
        findings.append("REALIZED_PNL_MISMATCH")
    if not _outcome_accounting_matches(
        outcome,
        event="settlement",
        resolved_at=settled_at,
        settlement_high_f=canonical_high,
        resolved_yes=canonical_resolved_yes,
        position_won=position_won,
        realized_pnl=expected_pnl,
        contracts=contracts,
    ):
        findings.append("OUTCOME_DIAGNOSTICS_MISMATCH")
    return findings


def _closed_accounting_findings(
    row: sqlite3.Row,
    outcome: dict[str, Any],
) -> list[str]:
    contracts = _finite_number(_row_value(row, "contracts"), minimum=0)
    cost = _finite_number(
        _row_value(row, "cost_per_contract"), minimum=0, maximum=1
    )
    exit_price = _finite_number(
        _row_value(row, "exit_price"), minimum=0, maximum=1
    )
    exit_fee = _finite_number(_row_value(row, "exit_fee_per_contract"), minimum=0)
    recorded_pnl = _finite_number(_row_value(row, "realized_pnl"))
    if (
        contracts in {None, 0.0}
        or cost is None
        or exit_price is None
        or exit_fee is None
        or recorded_pnl is None
    ):
        return []
    expected_pnl = closed_position_pnl(contracts, cost, exit_price, exit_fee)
    return [] if _close_number(recorded_pnl, expected_pnl) else ["REALIZED_PNL_MISMATCH"]


def _row_uses_current_maker_semantics(row: object) -> bool:
    return uses_current_maker_semantics(
        _row_value(row, "execution_model_version"),
        _row_value(row, "entry_mode"),
        _row_value(row, "fill_model"),
    )


def _logical_maker_authority_findings(
    group: LogicalPaperPosition,
    allocations_by_order: dict[int, list[sqlite3.Row]],
) -> list[str]:
    """Reconcile a v4 maker decision to immutable entry and lot evidence."""

    root = group.root
    lots = group.lots
    root_id = _positive_row_id(root.get("id"))
    if root_id is None:
        return ["EXEC_V4_LOGICAL_QUANTITY_INVALID"]
    findings: list[str] = []

    evidence = _json_object(root.get("fill_evidence_json"))
    if evidence.get("model") != "maker_allocator_price_time_v4":
        findings.append("EXEC_V4_EVIDENCE_MODEL_MISMATCH")

    requested = _finite_number(root.get("requested_contracts"), minimum=0)
    filled = _finite_number(root.get("filled_contracts"), minimum=0)
    row_remaining = _finite_number(root.get("remaining_contracts"), minimum=0)
    evidence_requested = _finite_number(
        evidence.get("requested_quantity"), minimum=0
    )
    evidence_filled = _finite_number(evidence.get("filled_quantity"), minimum=0)
    evidence_remaining = _finite_number(
        evidence.get("remaining_quantity"), minimum=0
    )
    allocation_quantities = [
        _finite_number(row["fill_quantity"], minimum=0)
        for row in allocations_by_order.get(root_id, [])
    ]
    if (
        requested in {None, 0.0}
        or filled is None
        or row_remaining is None
        or evidence_requested in {None, 0.0}
        or evidence_filled is None
        or evidence_remaining is None
        or any(quantity is None for quantity in allocation_quantities)
    ):
        findings.append("EXEC_V4_LOGICAL_QUANTITY_INVALID")
    else:
        allocation_filled = sum(
            quantity for quantity in allocation_quantities if quantity is not None
        )
        if not _close_number(filled, allocation_filled):
            findings.append("EXEC_V4_PARENT_FILLED_QUANTITY_MISMATCH")
        if not _close_number(filled, evidence_filled):
            findings.append("EXEC_V4_EVIDENCE_QUANTITY_MISMATCH")
        if not _close_number(requested, evidence_requested):
            findings.append("EXEC_V4_EVIDENCE_QUANTITY_MISMATCH")
        cancelled = root.get("cancelled_at") is not None
        authoritative_unfilled = evidence_remaining if cancelled else row_remaining
        if not cancelled and not _close_number(row_remaining, evidence_remaining):
            findings.append("EXEC_V4_REQUESTED_QUANTITY_BALANCE_MISMATCH")
        if cancelled and not _close_number(row_remaining, 0.0):
            findings.append("EXEC_V4_REQUESTED_QUANTITY_BALANCE_MISMATCH")
        if not _close_number(requested, filled + authoritative_unfilled):
            findings.append("EXEC_V4_REQUESTED_QUANTITY_BALANCE_MISMATCH")

    quote = _json_object(root.get("quote_snapshot_json"))
    entry_price = _finite_number(root.get("entry_price"), minimum=0, maximum=1)
    limit_price = _finite_number(root.get("limit_price"), minimum=0, maximum=1)
    fee = _finite_number(root.get("fee_per_contract"), minimum=0)
    cost = _finite_number(root.get("cost_per_contract"), minimum=0)
    quote_price = _finite_number(quote.get("limit_price"), minimum=0, maximum=1)
    quote_fee = _finite_number(quote.get("fee_per_contract"), minimum=0)
    quote_cost = _finite_number(quote.get("cost_per_contract"), minimum=0)
    quote_contracts = _finite_number(quote.get("contracts"), minimum=0)
    if (
        entry_price is None
        or limit_price is None
        or fee is None
        or cost is None
        or quote_price is None
        or quote_fee is None
        or quote_cost is None
        or quote_contracts in {None, 0.0}
        or requested in {None, 0.0}
    ):
        findings.append("EXEC_V4_ENTRY_QUOTE_INVALID")
    else:
        canonical_fee = quadratic_fee_average_per_contract(
            entry_price,
            requested,
            maker=True,
            series_ticker=str(root.get("market_ticker") or ""),
        )
        canonical_cost = entry_price + canonical_fee
        if any(
            not matches
            for matches in (
                _close_number(entry_price, limit_price),
                _close_number(entry_price, quote_price),
                _close_number(fee, canonical_fee),
                _close_number(fee, quote_fee),
                _close_number(cost, canonical_cost),
                _close_number(cost, quote_cost),
                _close_number(quote_contracts, requested),
            )
        ):
            findings.append("EXEC_V4_ENTRY_COST_MISMATCH")
        for lot in lots:
            if any(
                not _close_number(lot.get(field), root.get(field))
                for field in (
                    "entry_price",
                    "fee_per_contract",
                    "cost_per_contract",
                )
            ):
                findings.append("EXEC_V4_ENTRY_COST_MISMATCH")
                break

    terminal_statuses = {"PAPER_SETTLED", "PAPER_CLOSED"}
    resolved_quantity = 0.0
    resolved_quantity_valid = True
    for lot in lots:
        lot_contracts = _finite_number(lot.get("contracts"), minimum=0)
        if lot_contracts in {None, 0.0}:
            resolved_quantity_valid = False
        elif str(lot.get("status") or "") in terminal_statuses:
            resolved_quantity += lot_contracts
        if str(lot.get("status") or "") == "PAPER_CLOSED":
            execution = _json_object(lot.get("outcome_diagnostics_json")).get(
                "exit_execution"
            )
            executed = (
                _finite_number(execution.get("executed_quantity"), minimum=0)
                if isinstance(execution, dict)
                else None
            )
            if (
                lot_contracts not in {None, 0.0}
                and executed not in {None, 0.0}
                and not _close_number(lot_contracts, executed)
            ):
                findings.append("EXEC_V4_EXIT_QUANTITY_MISMATCH")

    if filled is not None:
        root_status = str(root.get("status") or "")
        if root_status in terminal_statuses:
            if not resolved_quantity_valid or not _close_number(
                resolved_quantity, filled
            ):
                findings.append("EXEC_V4_LOGICAL_RESOLVED_QUANTITY_MISMATCH")
        else:
            open_root_quantity = (
                _finite_number(root.get("contracts"), minimum=0)
                if root_status
                in {
                    "PAPER_FILLED",
                    "PAPER_PARTIALLY_FILLED",
                    "PAPER_PARTIAL_EXPIRED",
                }
                else 0.0
            )
            if (
                not resolved_quantity_valid
                or open_root_quantity is None
                or not _close_number(
                    resolved_quantity + open_root_quantity, filled
                )
            ):
                findings.append("EXEC_V4_LOGICAL_OPEN_QUANTITY_MISMATCH")
    return list(dict.fromkeys(findings))


def restate(db_path: Path) -> dict[str, Any]:
    """Build the immutable-evidence restatement report for one database."""

    with _connect_readonly(Path(db_path)) as conn:
        orders = conn.execute(
            "SELECT * FROM paper_orders WHERE status != 'REJECTED' ORDER BY created_at, id"
        ).fetchall()
        all_order_identity_rows = conn.execute(
            "SELECT id, market_ticker, target_date FROM paper_orders"
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

    all_orders_by_id = {
        order_id: row
        for row in all_order_identity_rows
        if (order_id := _positive_row_id(row["id"])) is not None
    }
    orders_by_id = {
        order_id: row
        for row in orders
        if (order_id := _positive_row_id(row["id"])) is not None
    }
    maker_candidate_ids_by_ticker: dict[str, set[int]] = defaultdict(set)
    for order_id, row in orders_by_id.items():
        if (
            _json_object(row["fill_evidence_json"]).get("model")
            == "maker_allocator_price_time_v4"
        ):
            maker_candidate_ids_by_ticker[str(row["market_ticker"])].add(order_id)

    settlement_checks: dict[int, sqlite3.Row] = {}
    settlement_findings_by_order: dict[int, list[str]] = defaultdict(list)
    global_settlement_findings: list[str] = []

    def attach_unowned_settlement_finding(
        check_row: sqlite3.Row,
        finding: str,
    ) -> None:
        ticker_value = _row_value(check_row, "market_ticker")
        ticker = _strict_text_identity(ticker_value)
        findings = [finding]
        if ticker is None:
            findings.append("SETTLEMENT_VERIFICATION_TICKER_INVALID")
            target_ids: set[int] = set()
        elif ticker not in maker_candidate_ids_by_ticker:
            findings.append("SETTLEMENT_VERIFICATION_TICKER_UNRESOLVABLE")
            target_ids = set()
        else:
            target_ids = maker_candidate_ids_by_ticker[ticker]
        if target_ids:
            for target_id in target_ids:
                for reason in findings:
                    _append_finding(settlement_findings_by_order[target_id], reason)
        else:
            for reason in findings:
                _append_finding(global_settlement_findings, reason)

    for check_row in settlement_rows:
        check_order_id = _positive_row_id(_row_value(check_row, "order_id"))
        if check_order_id is None:
            attach_unowned_settlement_finding(
                check_row, "SETTLEMENT_VERIFICATION_ORDER_ID_INVALID"
            )
            continue
        settlement_checks[check_order_id] = check_row
        order = all_orders_by_id.get(check_order_id)
        if order is None:
            attach_unowned_settlement_finding(
                check_row, "SETTLEMENT_VERIFICATION_ORDER_ID_UNRESOLVABLE"
            )
            continue
        check_ticker = _strict_text_identity(
            _row_value(check_row, "market_ticker")
        )
        if check_ticker is None:
            settlement_findings_by_order[check_order_id].append(
                "SETTLEMENT_VERIFICATION_TICKER_INVALID"
            )
        elif check_ticker != str(order["market_ticker"]):
            settlement_findings_by_order[check_order_id].append(
                "SETTLEMENT_VERIFICATION_TICKER_MISMATCH"
            )
        check_target_date = _strict_text_identity(
            _row_value(check_row, "target_date")
        )
        if check_target_date is None:
            settlement_findings_by_order[check_order_id].append(
                "SETTLEMENT_VERIFICATION_TARGET_DATE_INVALID"
            )
        elif check_target_date != str(order["target_date"]):
            settlement_findings_by_order[check_order_id].append(
                "SETTLEMENT_VERIFICATION_TARGET_DATE_MISMATCH"
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
        orders,
        allocation_rows,
        tape_rows,
        maker_claim_rows,
        known_order_ids=set(all_orders_by_id),
        known_order_tickers={
            order_id: str(row["market_ticker"])
            for order_id, row in all_orders_by_id.items()
        },
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
        ticker = str(row["market_ticker"])
        if order_id is not None:
            findings += settlement_findings_by_order.get(order_id, [])
        if evidence.get("model") == "maker_allocator_price_time_v4":
            findings += global_settlement_findings
            if str(row["status"]) == "PAPER_SETTLED":
                findings += _settled_accounting_findings(
                    row, outcome, settlement_check
                )
            elif str(row["status"]) == "PAPER_CLOSED":
                findings += _closed_accounting_findings(row, outcome)
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

    classes_by_order_id = {
        int(entry["order_id"]): entry
        for entry in classes
        if isinstance(entry.get("order_id"), int)
    }
    for group in group_logical_positions(orders):
        root = group.root
        if not _row_uses_current_maker_semantics(root):
            continue
        lot_ids = [
            order_id
            for lot in group.lots
            if (order_id := _positive_row_id(lot.get("id"))) is not None
        ]
        group_findings = _logical_maker_authority_findings(
            group, allocations_by_order
        )
        if not group.valid:
            _append_finding(
                group_findings, "EXEC_V4_LOGICAL_INTEGRITY_INVALID"
            )
        lot_has_findings = any(
            classes_by_order_id.get(order_id, {}).get("findings")
            for order_id in lot_ids
        )
        if not group_findings and not lot_has_findings:
            continue
        if lot_has_findings:
            _append_finding(
                group_findings, "EXEC_V4_LOGICAL_LOT_UNVERIFIABLE"
            )
        for order_id in lot_ids:
            entry = classes_by_order_id.get(order_id)
            if entry is None:
                continue
            for finding in group_findings:
                _append_finding(entry["findings"], finding)
            entry["verification"] = UNVERIFIABLE

    for entry in classes:
        status = str(entry["status"])
        if status not in resolved_statuses:
            continue
        verification = str(entry["verification"])
        profile = str(entry["risk_profile"])
        order_id = _positive_row_id(entry["order_id"])
        source_row = orders_by_id.get(order_id) if order_id is not None else None
        realized = _finite_number(
            _row_value(source_row, "realized_pnl") if source_row is not None else None
        )
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
