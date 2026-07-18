"""Read-only historical restatement under versioned execution semantics.

Audit 2026-07-13 Batch D: the original journal is immutable; this module
re-examines every order's ENTRY and EXIT evidence under the corrected
execution model (``EXECUTION_MODEL_VERSION``) and reports, side by side, the
legacy headline P&L and the verification-classed view. It never writes to the
database it inspects.

Classification rules (conservative -- insufficient evidence is UNVERIFIABLE,
never "filled by assumption"):

Entry evidence
  - ``maker_allocator_price_time_v2`` fill evidence: VERIFIED (single-aggressor
    direction, once-only volume allocation, persisted claims).
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
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from ._util import _json_object
from .account import ACCOUNTING_POLICY_VERSION
from .maker_fills import EXECUTION_MODEL_VERSION, depth_observation_is_contemporaneous

# The taker-entry displayed-ask sizing cap landed 2026-07-10; taker entries
# before it could exceed displayed liquidity.
TAKER_SIZING_FIX_DATE = "2026-07-10"

VERIFIED = "VERIFIED"
UNVERIFIABLE = "UNVERIFIABLE"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _entry_findings(
    row: sqlite3.Row,
    evidence: dict[str, Any],
    duplicate_trade_ids: set[str],
    v3_findings: list[str] | None = None,
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
    if model == "maker_allocator_price_time_v3":
        return list(v3_findings or [])
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
    execution = outcome.get("exit_execution") or {}
    try:
        executed = float(execution.get("executed_quantity"))
    except (TypeError, ValueError):
        return ["EXIT_DEPTH_UNVERIFIED"]
    depth_value = execution.get(
        "displayed_depth", execution.get("displayed_bid_size")
    )
    try:
        displayed_depth = float(depth_value)
    except (TypeError, ValueError):
        return ["EXIT_DEPTH_UNVERIFIED"]
    if displayed_depth + 1e-9 < executed:
        return ["EXIT_DEPTH_INSUFFICIENT"]
    try:
        closed_at = row["closed_at"]
    except (IndexError, KeyError, TypeError):
        closed_at = None
    if not depth_observation_is_contemporaneous(
        execution.get("observed_at"), closed_at
    ):
        return ["EXIT_DEPTH_STALE"]
    if (
        executed <= 0
        or displayed_depth <= 0
        or not execution.get("source")
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
        settlement_checks = {
            int(check_row["order_id"]): str(check_row["verification_status"])
            for check_row in conn.execute(
                "SELECT order_id, verification_status FROM paper_settlement_verifications"
            ).fetchall()
        }
        has_v3_allocations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='paper_maker_allocations'"
        ).fetchone() is not None
        allocation_rows = (
            conn.execute("SELECT * FROM paper_maker_allocations").fetchall()
            if has_v3_allocations
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

    allocations_by_order: dict[int, list[sqlite3.Row]] = defaultdict(list)
    capital_consumption: dict[str, float] = defaultdict(float)
    for allocation_row in allocation_rows:
        allocations_by_order[int(allocation_row["order_id"])].append(allocation_row)
        if not int(allocation_row["counterfactual"] or 0):
            capital_consumption[str(allocation_row["trade_id"])] += float(
                allocation_row["queue_quantity"] or 0.0
            ) + float(allocation_row["fill_quantity"] or 0.0)
    tape_by_id = {str(tape_row["trade_id"]): tape_row for tape_row in tape_rows}
    overclaimed_trade_ids = {
        trade_id
        for trade_id, consumed in capital_consumption.items()
        if trade_id not in tape_by_id
        or consumed > float(tape_by_id[trade_id]["count"] or 0.0) + 1e-9
    }

    v3_findings_by_order: dict[int, list[str]] = {}
    for row in orders:
        order_id = int(row["id"])
        evidence = _json_object(row["fill_evidence_json"])
        if evidence.get("model") != "maker_allocator_price_time_v3":
            continue
        findings: list[str] = []
        order_allocations = allocations_by_order.get(order_id, [])
        if not order_allocations:
            findings.append("EXEC_V3_ALLOCATION_MISSING")
        expected_fill = float(
            row["filled_contracts"]
            if "filled_contracts" in row.keys() and row["filled_contracts"] is not None
            else row["contracts"]
        )
        recorded_fill = sum(
            float(item["fill_quantity"] or 0.0) for item in order_allocations
        )
        if abs(recorded_fill - expected_fill) > 1e-9:
            findings.append("EXEC_V3_FILL_QUANTITY_MISMATCH")
        side = str(row["side"] or "YES").upper()
        limit_price = float(
            row["limit_price"]
            if row["limit_price"] is not None
            else row["entry_price"]
        )
        queue_price = float(
            row["entry_bid"]
            if row["entry_bid"] is not None
            else limit_price
        )
        created_at = str(row["created_at"] or "")
        for allocation_row in order_allocations:
            trade_id = str(allocation_row["trade_id"])
            tape = tape_by_id.get(trade_id)
            if tape is None:
                findings.append("EXEC_V3_TAPE_MISSING")
                continue
            if str(allocation_row["maker_side"]).upper() != side:
                findings.append("EXEC_V3_DIRECTION_INVALID")
            if str(allocation_row["trade_created_at"]) <= created_at:
                findings.append("EXEC_V3_TRADE_NOT_LATER")
            side_price = float(allocation_row["side_price"])
            queue_quantity = float(allocation_row["queue_quantity"] or 0.0)
            fill_quantity = float(allocation_row["fill_quantity"] or 0.0)
            if (
                fill_quantity > 0
                and side_price > limit_price + 1e-9
            ) or (
                queue_quantity > 0
                and side_price > queue_price + 1e-9
            ):
                findings.append("EXEC_V3_PRICE_INVALID")
            if trade_id in overclaimed_trade_ids:
                findings.append("EXEC_V3_VOLUME_OVERCLAIMED")
        v3_findings_by_order[order_id] = list(dict.fromkeys(findings))

    # A public trade id credited by more than one capital-consuming order's
    # evidence is the double-credit signature (production 226/227, 261/262).
    trade_id_owners: dict[str, set[int]] = defaultdict(set)
    evidences: dict[int, dict[str, Any]] = {}
    for row in orders:
        evidence = _json_object(row["fill_evidence_json"])
        evidences[int(row["id"])] = evidence
        if evidence.get("research_shadow") or evidence.get("counterfactual"):
            continue
        for trade_id in evidence.get("trade_ids") or []:
            trade_id_owners[str(trade_id)].add(int(row["id"]))
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
        order_id = int(row["id"])
        evidence = evidences[order_id]
        # A partial-close lot carries no fill evidence of its own -- its entry
        # was executed (and verified) on the parent order it split from.
        parent_id = row["parent_order_id"] if "parent_order_id" in row.keys() else None
        if parent_id:
            evidence = evidences.get(int(parent_id), {})
        entry_evidence_order_id = int(parent_id) if parent_id else order_id
        outcome = _json_object(row["outcome_diagnostics_json"])
        findings = _entry_findings(
            row,
            evidence,
            duplicate_trade_ids,
            v3_findings_by_order.get(entry_evidence_order_id),
        )
        findings += _exit_findings(row, outcome)
        settlement_check = settlement_checks.get(order_id)
        if settlement_check == "MISMATCH":
            findings.append("SETTLEMENT_MISMATCH")
        verification = UNVERIFIABLE if findings else VERIFIED
        realized = float(row["realized_pnl"] or 0.0)
        profile = str(row["risk_profile"] or "live")
        classes.append(
            {
                "order_id": order_id,
                "market_ticker": str(row["market_ticker"]),
                "target_date": str(row["target_date"]),
                "status": str(row["status"]),
                "risk_profile": profile,
                "verification": verification,
                "findings": findings,
                "realized_pnl": round(realized, 4)
                if str(row["status"]) in resolved_statuses
                else None,
            }
        )
        if str(row["status"]) in resolved_statuses:
            totals[verification]["orders"] += 1
            totals[verification]["realized_pnl"] += realized
            per_profile[profile][verification]["orders"] += 1
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
