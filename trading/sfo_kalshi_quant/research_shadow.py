from __future__ import annotations

import sqlite3
from typing import Any

from ._util import _optional_float
from .db import PaperStore
from .settlement_truth import (
    normalize_settlement_truth,
    row_resolves_yes as _row_resolves_yes,
    settlement_for_market,
)


def build_research_shadow_report(
    store: PaperStore,
    *,
    settlements: dict[object, float] | None = None,
    limit: int = 10000,
) -> dict[str, Any]:
    normalized_settlements = normalize_settlement_truth(settlements or {})
    try:
        rows = _shadow_rows_with_paper(store, limit=limit)
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if rows is None:
        return {
            "available": False,
            "reason": "research_shadow_orders table is not available yet.",
        }

    paper_executed = _new_bucket()
    shadow_hold = _new_bucket()
    current_policy = _new_bucket()
    sampled_orders = 0
    linked_orders = 0

    for row in rows:
        if int(row["sampled"] or 0):
            sampled_orders += 1
        if row["paper_id"] is not None:
            linked_orders += 1
            paper_contracts = _as_float(row["paper_contracts"], 0.0)
            paper_cost = _as_float(row["paper_cost_per_contract"], 0.0)
            paper_pnl = _optional_float(row["paper_realized_pnl"])
            _add_bucket(
                paper_executed,
                contracts=paper_contracts,
                capital=paper_contracts * paper_cost,
                pnl=paper_pnl,
                won=paper_pnl > 0 if paper_pnl is not None else None,
                completed=paper_pnl is not None,
            )
            if paper_pnl is not None:
                _add_bucket(
                    current_policy,
                    contracts=paper_contracts,
                    capital=paper_contracts * paper_cost,
                    pnl=paper_pnl,
                    won=paper_pnl > 0,
                    completed=True,
                )

        target_date = str(row["target_date"])
        settlement_high = settlement_for_market(
            normalized_settlements, str(row["market_ticker"]), target_date
        )
        if settlement_high is not None:
            resolved_yes = _row_resolves_yes(row, settlement_high)
            side = _row_side(row)
            won = resolved_yes if side == "YES" else not resolved_yes
            contracts = _as_float(row["contracts"], 0.0)
            cost = _as_float(row["cost_per_contract"], 0.0)
            pnl = contracts * ((1.0 - cost) if won else -cost)
            _add_bucket(
                shadow_hold,
                contracts=contracts,
                capital=contracts * cost,
                pnl=pnl,
                won=won,
                completed=True,
            )

    return {
        "available": True,
        "mode": "ghost_plus_sampled_paper",
        "summary": {
            "shadow_orders": len(rows),
            "sampled_orders": sampled_orders,
            "linked_paper_orders": linked_orders,
            "settlement_days": len(normalized_settlements),
        },
        "paper_executed": _final_bucket(paper_executed),
        "shadow_hold_to_settlement": _final_bucket(shadow_hold),
        "shadow_current_exit_policy": _final_bucket(current_policy),
    }


def _shadow_rows_with_paper(store: PaperStore, *, limit: int) -> list[sqlite3.Row] | None:
    with store.connect() as conn:
        exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'research_shadow_orders'
            """
        ).fetchone()
        if not exists:
            return None
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT
                s.*,
                p.id AS paper_id,
                p.status AS paper_status,
                p.contracts AS paper_contracts,
                p.cost_per_contract AS paper_cost_per_contract,
                p.realized_pnl AS paper_realized_pnl,
                p.closed_at AS paper_closed_at,
                p.settled_at AS paper_settled_at,
                p.exit_price AS paper_exit_price,
                p.exit_fee_per_contract AS paper_exit_fee_per_contract
            FROM research_shadow_orders s
            LEFT JOIN paper_orders p ON p.id = s.linked_paper_order_id
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def _new_bucket() -> dict[str, float]:
    return {
        "trades": 0.0,
        "completed_trades": 0.0,
        "wins": 0.0,
        "losses": 0.0,
        "contracts": 0.0,
        "capital_at_risk": 0.0,
        "realized_pnl": 0.0,
    }


def _add_bucket(
    bucket: dict[str, float],
    *,
    contracts: float,
    capital: float,
    pnl: float | None,
    won: bool | None,
    completed: bool,
) -> None:
    bucket["trades"] += 1.0
    bucket["contracts"] += contracts
    bucket["capital_at_risk"] += capital
    if completed and pnl is not None and won is not None:
        bucket["completed_trades"] += 1.0
        bucket["realized_pnl"] += pnl
        if won:
            bucket["wins"] += 1.0
        else:
            bucket["losses"] += 1.0


def _final_bucket(bucket: dict[str, float]) -> dict[str, Any]:
    trades = int(bucket["trades"])
    completed = int(bucket["completed_trades"])
    capital = bucket["capital_at_risk"]
    pnl = bucket["realized_pnl"]
    return {
        "trades": trades,
        "completed_trades": completed,
        "wins": int(bucket["wins"]),
        "losses": int(bucket["losses"]),
        "contracts": round(bucket["contracts"], 4),
        "capital_at_risk": round(capital, 4),
        "realized_pnl": round(pnl, 4),
        "roi": round(pnl / capital, 6) if capital > 0 and completed else None,
    }


def _row_side(row: sqlite3.Row) -> str:
    side = row["side"]
    if side and str(side).upper() in {"YES", "NO"}:
        return str(side).upper()
    return "NO" if "NO" in str(row["action"]).upper() else "YES"


def _as_float(value: object, default: float) -> float:
    parsed = _optional_float(value)
    return default if parsed is None else parsed
