from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .._util import (
    _date_from_string,
    _db_table_exists,
    _env_float,
    _json_list,
    _json_object,
    _parse_timestamp,
    _round,
    _row_value as _sqlite_row_value,
    _to_float,
)
from ..config import strategy_config_for_profile, normalize_risk_profile_name
from ..db import PaperStore
from ..exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
    convergence_take_profit_net,
    decide_exit,
    exit_bid_for_net,
)
from ..fees import quadratic_fee_average_per_contract
from ..settlement_day import settlement_today
from . import DEFAULT_MODEL_VETO_BUFFER, DEFAULT_MODEL_VETO_MAX_LOSS_PCT
from .status_alerts import _why_trade_good


def _paper_payload(db_path: Path) -> dict[str, Any]:
    monitor = _paper_monitor_config()
    empty = {
        "available": False,
        "monitor": monitor,
        "summary": {
            "open_positions": 0,
            "published_open_positions": 0,
            "hidden_open_positions": 0,
            "pending_limit_orders": 0,
            "published_pending_limit_orders": 0,
            "hidden_pending_limit_orders": 0,
            "pending_limit_risk": 0.0,
            "duplicate_open_groups": 0,
            "largest_duplicate_open_group": 0,
            "unresolved_past_targets": [],
            "latest_monitor_action_at": None,
            "closed_positions": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": None,
            "marked_open_positions": 0,
            "open_risk": 0.0,
            "open_value": None,
            "win_count": 0,
            "loss_count": 0,
        },
        "open_positions": [],
        "pending_limit_orders": [],
        "closed_positions": [],
        "recent_monitor_actions": [],
        "diagnostics": _empty_paper_diagnostics(),
        "profiles": [],
    }
    if not db_path.exists():
        return {**empty, "reason": f"Paper-trading DB not found: {db_path}"}
    if not _db_table_exists(db_path, "paper_orders"):
        return {**empty, "reason": "paper_orders table is not available yet."}

    store = PaperStore(db_path, init=False)
    summary = store.market_backtest_summary()
    monitor_marks = _latest_monitor_marks(db_path)
    decision_marks = _latest_position_marks(db_path)
    has_monitor_snapshots = _db_table_exists(db_path, "paper_monitor_snapshots")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        open_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status = 'PAPER_FILLED'
              AND settled_at IS NULL
              AND closed_at IS NULL
            ORDER BY created_at DESC
            LIMIT 30
            """
        ).fetchall()
        pending_limit_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status = 'PAPER_LIMIT_RESTING'
              AND settled_at IS NULL
              AND closed_at IS NULL
            ORDER BY created_at DESC
            LIMIT 30
            """
        ).fetchall()
        pending_limit_summary = conn.execute(
            """
            SELECT COUNT(*) AS pending_orders,
                   COALESCE(SUM(contracts * cost_per_contract), 0.0) AS pending_risk
            FROM paper_orders
            WHERE status = 'PAPER_LIMIT_RESTING'
              AND settled_at IS NULL
              AND closed_at IS NULL
            """
        ).fetchone()
        closed_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
              AND realized_pnl IS NOT NULL
            ORDER BY COALESCE(closed_at, settled_at, created_at) DESC
            LIMIT 30
            """
        ).fetchall()
        closed_action_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE closed_at IS NOT NULL OR settled_at IS NOT NULL
            ORDER BY COALESCE(closed_at, settled_at, created_at) DESC
            LIMIT 12
            """
        ).fetchall()
        monitor_rows = []
        if has_monitor_snapshots:
            # Keep only the LATEST snapshot per order. The monitor writes one row
            # per open order every ~2-minute cycle, so without this dedup a single
            # position that HOLDs for a few cycles shows up as several identical
            # "actions" (the repeated 0.59/0.40/0.11 @ 0.99 the owner saw). The
            # window-function filter collapses those to one inspection mark per
            # order. See docs/trading_engine_diagnosis_2026-06-16.md (Finding 5).
            monitor_rows = conn.execute(
                """
                SELECT m.*, p.label, p.risk_profile
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY order_id
                               ORDER BY created_at DESC, id DESC
                           ) AS rn
                    FROM paper_monitor_snapshots
                ) m
                LEFT JOIN paper_orders p ON p.id = m.order_id
                WHERE m.rn = 1
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 12
                """
            ).fetchall()
        duplicate_rows = conn.execute(
            """
            SELECT target_date,
                   COALESCE(risk_profile, 'live') AS risk_profile,
                   market_ticker,
                   UPPER(COALESCE(side, 'YES')) AS side,
                   COUNT(*) AS open_orders
            FROM paper_orders
            WHERE status = 'PAPER_FILLED'
              AND settled_at IS NULL
              AND closed_at IS NULL
            GROUP BY target_date,
                     COALESCE(risk_profile, 'live'),
                     market_ticker,
                     UPPER(COALESCE(side, 'YES'))
            HAVING COUNT(*) > 1
            ORDER BY open_orders DESC, target_date, risk_profile, market_ticker
            LIMIT 5
            """
        ).fetchall()
        target_rows = conn.execute(
            """
            SELECT target_date, COUNT(*) AS open_orders
            FROM paper_orders
            WHERE status = 'PAPER_FILLED'
              AND settled_at IS NULL
              AND closed_at IS NULL
            GROUP BY target_date
            ORDER BY target_date
            """
        ).fetchall()
        scanning_profiles = []
        if _db_table_exists(db_path, "decision_snapshots"):
            scanning_profiles = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT DISTINCT COALESCE(risk_profile, 'unknown')
                    FROM decision_snapshots
                    WHERE created_at >= datetime('now', '-7 days')
                    ORDER BY 1
                    """
                ).fetchall()
            ]
        profile_rows = conn.execute(
            """
            SELECT COALESCE(risk_profile, 'unknown') AS risk_profile,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            THEN 1 ELSE 0 END) AS orders,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            AND realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            THEN COALESCE(realized_pnl, 0.0) ELSE 0.0 END) AS realized_pnl,
                   SUM(CASE WHEN status = 'PAPER_FILLED'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN 1 ELSE 0 END) AS open_positions,
                   SUM(CASE WHEN status = 'PAPER_FILLED'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN contracts * cost_per_contract ELSE 0.0 END) AS open_risk,
                   SUM(CASE WHEN status = 'PAPER_LIMIT_RESTING'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN 1 ELSE 0 END) AS pending_limit_orders,
                   SUM(CASE WHEN status = 'PAPER_LIMIT_RESTING'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN contracts * cost_per_contract ELSE 0.0 END) AS pending_limit_risk,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                             AND realized_pnl IS NOT NULL
                            THEN contracts * cost_per_contract ELSE 0.0 END) AS capital_resolved
            FROM paper_orders
            WHERE status != 'REJECTED'
            GROUP BY COALESCE(risk_profile, 'unknown')
            ORDER BY risk_profile
            """
        ).fetchall()

    open_positions = [
        _paper_row(row, _position_mark_for(row, monitor_marks, decision_marks), monitor)
        for row in open_rows
    ]
    # Mark resting limit orders with the latest scanned bid/ask too, so the
    # card can show the current market price and how far the ask is from the
    # resting limit. The monitor skips resting orders, so there is no monitor
    # mark; the decision-snapshot mark (recorded every scan) is the live price.
    pending_limit_orders = [
        _paper_row(
            row,
            decision_marks.get((row["market_ticker"], _side_from_row(row))),
            monitor,
        )
        for row in pending_limit_rows
    ]
    closed_positions = [_paper_row(row, None, monitor) for row in closed_rows]
    monitor_action_rows = [_paper_monitor_snapshot_row(row) for row in monitor_rows] + [
        _paper_action_row(row) for row in closed_action_rows
    ]
    monitor_action_rows = sorted(
        monitor_action_rows,
        key=lambda row: str(row.get("time") or ""),
        reverse=True,
    )
    open_action_rows = [_paper_open_action_row(row) for row in open_rows]
    pending_action_rows = [_paper_limit_action_row(row) for row in pending_limit_rows]
    open_reserve = min(4, len(open_action_rows) + len(pending_action_rows))
    action_rows = (
        monitor_action_rows[: max(0, 12 - open_reserve)]
        + pending_action_rows[:open_reserve]
        + open_action_rows[: max(0, open_reserve - len(pending_action_rows))]
    )
    action_rows = sorted(action_rows, key=lambda row: str(row.get("time") or ""), reverse=True)[:12]
    duplicate_groups = [_duplicate_group_row(row) for row in duplicate_rows]
    today = settlement_today()
    unresolved_past_targets = [
        {
            "target_date": str(row["target_date"]),
            "open_orders": int(row["open_orders"]),
        }
        for row in target_rows
        if _date_from_string(row["target_date"]) is not None
        and _date_from_string(row["target_date"]) < today
    ]
    marked_open_positions = [
        row for row in open_positions if row.get("unrealized_pnl") is not None
    ]
    unrealized_pnl = (
        _round(sum(_to_float(row.get("unrealized_pnl")) for row in marked_open_positions), 2)
        if marked_open_positions
        else None
    )
    open_value = (
        _round(sum(_to_float(row.get("current_value")) for row in marked_open_positions), 2)
        if marked_open_positions
        else None
    )
    win_count = int(_to_float(summary.get("wins"), default=0.0))
    loss_count = int(_to_float(summary.get("losses"), default=0.0))
    pending_limit_count = int(pending_limit_summary["pending_orders"] or 0) if pending_limit_summary else 0
    pending_limit_risk = _to_float(pending_limit_summary["pending_risk"]) if pending_limit_summary else 0.0
    return {
        "available": True,
        "monitor": monitor,
        "summary": {
            "open_positions": int(summary["open_orders"]),
            "published_open_positions": len(open_positions),
            "hidden_open_positions": max(0, int(summary["open_orders"]) - len(open_positions)),
            "pending_limit_orders": pending_limit_count,
            "published_pending_limit_orders": len(pending_limit_orders),
            "hidden_pending_limit_orders": max(0, pending_limit_count - len(pending_limit_orders)),
            "pending_limit_risk": _round(pending_limit_risk, 2),
            "duplicate_open_groups": len(duplicate_groups),
            "largest_duplicate_open_group": max(
                [row["open_orders"] for row in duplicate_groups],
                default=0,
            ),
            "unresolved_past_targets": unresolved_past_targets,
            "latest_opened_at": open_rows[0]["created_at"] if open_rows else None,
            "latest_monitor_action_at": (
                monitor_action_rows[0].get("time") if monitor_action_rows else None
            ),
            "closed_positions": int(summary["orders"]),
            "realized_pnl": _round(summary["realized_pnl"], 2),
            "unrealized_pnl": unrealized_pnl,
            "marked_open_positions": len(marked_open_positions),
            "open_risk": _round(summary["open_capital_at_risk"], 2),
            "open_value": open_value,
            "capital_at_risk": _round(summary["capital_at_risk"], 2),
            "roi": _round(summary["roi"], 4),
            "hit_rate": _round(summary["hit_rate"], 4),
            "win_count": win_count,
            "loss_count": loss_count,
        },
        "open_positions": open_positions,
        "pending_limit_orders": pending_limit_orders,
        "closed_positions": closed_positions,
        "recent_monitor_actions": action_rows,
        "duplicate_open_groups": duplicate_groups,
        "diagnostics": _paper_diagnostics(db_path),
        "profiles": _profiles_with_scanners(
            [_profile_summary_row(row) for row in profile_rows],
            scanning_profiles,
        ),
    }


def _paper_diagnostics(db_path: Path) -> dict[str, Any]:
    if not db_path.exists() or not _db_table_exists(db_path, "paper_orders"):
        return _empty_paper_diagnostics()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status != 'REJECTED'
            """
        ).fetchall()

    resolved = [
        row
        for row in rows
        if row["realized_pnl"] is not None and row["status"] != "PAPER_EXPIRED"
    ]
    return {
        "resolved_positions": len(resolved),
        "by_profile": _paper_group_diagnostics(resolved, lambda row: _row_risk_profile(row) or "unknown"),
        "by_side": _paper_group_diagnostics(resolved, _side_from_row),
        "by_exit_reason": _paper_group_diagnostics(resolved, _paper_exit_reason),
        "worst_segments": _worst_paper_segments(resolved),
    }


def _empty_paper_diagnostics() -> dict[str, Any]:
    return {
        "resolved_positions": 0,
        "by_profile": {},
        "by_side": {},
        "by_exit_reason": {},
        "worst_segments": [],
    }


def _paper_group_diagnostics(
    rows: list[sqlite3.Row],
    key_fn,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row)), []).append(row)
    return {key: _paper_group_summary(group) for key, group in sorted(groups.items())}


def _paper_group_summary(rows: list[sqlite3.Row]) -> dict[str, Any]:
    wins = sum(1 for row in rows if _paper_order_won(row))
    losses = sum(1 for row in rows if _paper_order_decided(row) and not _paper_order_won(row))
    pnl = sum(_to_float(row["realized_pnl"]) for row in rows)
    capital = sum(_to_float(row["contracts"]) * _to_float(row["cost_per_contract"]) for row in rows)
    return {
        "resolved": len(rows),
        "wins": wins,
        "losses": losses,
        "realized_pnl": _round(pnl, 2),
        "capital_resolved": _round(capital, 2),
        "roi": _round(pnl / capital, 4) if capital > 0 else None,
        "hit_rate": _round(wins / (wins + losses), 4) if (wins + losses) else None,
    }


def _worst_paper_segments(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (_row_risk_profile(row) or "unknown", _side_from_row(row), _paper_exit_reason(row))
        grouped.setdefault(key, []).append(row)
    segments = []
    for (profile, side, exit_reason), group in grouped.items():
        summary = _paper_group_summary(group)
        segments.append(
            {
                "risk_profile": profile,
                "side": side,
                "exit_reason": exit_reason,
                **summary,
            }
        )
    segments.sort(key=lambda row: (_to_float(row.get("realized_pnl")), -int(row.get("resolved") or 0)))
    return segments[:6]


def _paper_exit_reason(row: sqlite3.Row) -> str:
    if row["status"] == "PAPER_EXPIRED":
        return "limit_expired"
    if row["closed_at"]:
        return "monitor_exit"
    if row["settled_at"]:
        return "held_to_settlement"
    return "unresolved"


def _paper_order_won(row: sqlite3.Row) -> bool:
    try:
        resolved_yes = row["resolved_yes"]
    except (IndexError, KeyError):
        resolved_yes = None
    if resolved_yes is None:
        return _to_float(row["realized_pnl"]) > 0.0
    side = _side_from_row(row)
    return bool(resolved_yes) if side == "YES" else not bool(resolved_yes)


def _paper_order_decided(row: sqlite3.Row) -> bool:
    try:
        resolved_yes = row["resolved_yes"]
    except (IndexError, KeyError):
        resolved_yes = None
    if resolved_yes is not None:
        return True
    return abs(_to_float(row["realized_pnl"])) > 1e-9


def _profiles_with_scanners(
    profiles: list[dict[str, Any]],
    scanning_profiles: list[str],
) -> list[dict[str, Any]]:
    """A profile that scans but never trades must still appear in the lab —
    'balanced placed nothing while fast-feedback lost' is the key diagnostic."""

    present = {row["risk_profile"] for row in profiles}
    for name in scanning_profiles:
        if name in present:
            continue
        profiles.append(
            {
                "risk_profile": name,
                "orders": 0,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "hit_rate": None,
                "realized_pnl": 0.0,
                "roi": None,
                "open_positions": 0,
                "open_risk": 0.0,
                "pending_limit_orders": 0,
                "pending_limit_risk": 0.0,
            }
        )
    profiles.sort(key=lambda row: row["risk_profile"])
    return profiles


def _profile_summary_row(row: sqlite3.Row) -> dict[str, Any]:
    resolved = int(row["resolved"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    pnl = _to_float(row["realized_pnl"])
    capital = _to_float(row["capital_resolved"])
    return {
        "risk_profile": str(row["risk_profile"]),
        "orders": int(row["orders"] or 0),
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "hit_rate": _round(wins / (wins + losses), 4) if (wins + losses) else None,
        "realized_pnl": _round(pnl, 2),
        "roi": _round(pnl / capital, 4) if capital > 0 else None,
        "open_positions": int(row["open_positions"] or 0),
        "open_risk": _round(_to_float(row["open_risk"]), 2),
        "pending_limit_orders": int(row["pending_limit_orders"] or 0),
        "pending_limit_risk": _round(_to_float(row["pending_limit_risk"]), 2),
    }


def _row_risk_profile(row: sqlite3.Row) -> str | None:
    try:
        value = row["risk_profile"]
    except (IndexError, KeyError):
        return None
    return str(value) if value else None


def _latest_monitor_marks(db_path: Path) -> dict[int, dict[str, Any]]:
    if not db_path.exists() or not _db_table_exists(db_path, "paper_monitor_snapshots"):
        return {}
    marks: dict[int, dict[str, Any]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM paper_monitor_snapshots
            WHERE live_bid IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 5000
            """
        ).fetchall()
    for row in rows:
        order_id = int(row["order_id"])
        if order_id in marks:
            continue
        marks[order_id] = {
            "source": "paper_monitor_snapshot",
            "snapshot_id": row["id"],
            "created_at": row["created_at"],
            "bid": _round(row["live_bid"], 4),
            "ask": None,
            "bid_size": None,
            "ask_size": None,
            "spread": None,
            "market_probability": None,
            "quality_score": None,
            "monitor_action": row["action"],
            "monitor_reason": row["reason"],
        }
    return marks


def _latest_position_marks(db_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not db_path.exists() or not _db_table_exists(db_path, "decision_snapshots"):
        return {}
    marks: dict[tuple[str, str], dict[str, Any]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, created_at, market_ticker, side, yes_bid, yes_ask,
                   entry_bid, entry_ask, entry_bid_size, entry_ask_size,
                   spread, probability, model_probability, market_probability,
                   trade_quality_score
            FROM decision_snapshots
            ORDER BY created_at DESC, id DESC
            LIMIT 5000
            """
        ).fetchall()

    now = datetime.now(UTC)
    for row in rows:
        side = _side_from_row(row)
        key = (row["market_ticker"], side)
        if key in marks:
            continue
        bid = row["entry_bid"]
        ask = row["entry_ask"]
        if side == "YES":
            bid = row["yes_bid"] if bid is None else bid
            ask = row["yes_ask"] if ask is None else ask
        marks[key] = {
            "source": "latest_decision_snapshot",
            "snapshot_id": row["id"],
            "created_at": row["created_at"],
            "bid": _round(bid, 4),
            "ask": _round(ask, 4),
            "bid_size": _round(row["entry_bid_size"], 4),
            "ask_size": _round(row["entry_ask_size"], 4),
            "spread": _round(row["spread"], 4),
            "market_probability": _round(row["market_probability"], 4),
            "model_side_probability": _fresh_model_side_probability(row, now),
            "quality_score": _round(row["trade_quality_score"], 1),
        }
    return marks


def _fresh_model_side_probability(row: sqlite3.Row, now: datetime) -> float | None:
    created_at = _parse_timestamp(row["created_at"])
    if created_at is None or now - created_at > timedelta(minutes=90):
        return None
    value = row["model_probability"] if row["model_probability"] is not None else row["probability"]
    if value is None:
        return None
    parsed = _to_float(value, default=math.nan)
    if not math.isfinite(parsed):
        return None
    return max(0.0, min(1.0, parsed))


def _position_mark_for(
    row: sqlite3.Row,
    monitor_marks: dict[int, dict[str, Any]],
    decision_marks: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    side = _side_from_row(row)
    decision_mark = decision_marks.get((row["market_ticker"], side))
    monitor_mark = monitor_marks.get(int(row["id"]))
    if monitor_mark is None:
        return decision_mark
    if decision_mark is None:
        return monitor_mark
    return {**decision_mark, **monitor_mark}


def _side_from_row(row: sqlite3.Row) -> str:
    try:
        side = row["side"]
    except (IndexError, KeyError):
        side = None
    if side:
        normalized = str(side).upper()
        if normalized in {"YES", "NO"}:
            return normalized
    try:
        action = str(row["action"]).upper()
    except (IndexError, KeyError):
        return "YES"
    return "NO" if "NO" in action else "YES"


def _paper_monitor_config() -> dict[str, Any]:
    take_profit = _env_float("PAPER_TAKE_PROFIT_PCT")
    stop_loss = _env_float("PAPER_STOP_LOSS_PCT")
    yes_take_profit = _env_float("PAPER_YES_TAKE_PROFIT_PCT")
    yes_stop_loss = _env_float("PAPER_YES_STOP_LOSS_PCT")
    no_take_profit = _env_float("PAPER_NO_TAKE_PROFIT_PCT")
    no_stop_loss = _env_float("PAPER_NO_STOP_LOSS_PCT")
    model_veto_max_loss = _env_float("PAPER_MODEL_VETO_MAX_LOSS_PCT")
    model_veto_buffer = _env_float("PAPER_MODEL_VETO_BUFFER")
    return {
        "take_profit_pct": take_profit if take_profit is not None else DEFAULT_TAKE_PROFIT_PCT,
        "stop_loss_pct": stop_loss if stop_loss is not None else DEFAULT_STOP_LOSS_PCT,
        "yes_take_profit_pct": (
            yes_take_profit if yes_take_profit is not None else DEFAULT_YES_TAKE_PROFIT_PCT
        ),
        "yes_stop_loss_pct": yes_stop_loss if yes_stop_loss is not None else DEFAULT_YES_STOP_LOSS_PCT,
        "no_take_profit_pct": no_take_profit if no_take_profit is not None else DEFAULT_NO_TAKE_PROFIT_PCT,
        "no_stop_loss_pct": no_stop_loss if no_stop_loss is not None else DEFAULT_NO_STOP_LOSS_PCT,
        "model_veto_max_loss_pct": (
            model_veto_max_loss
            if model_veto_max_loss is not None
            else DEFAULT_MODEL_VETO_MAX_LOSS_PCT
        ),
        "model_veto_buffer": model_veto_buffer if model_veto_buffer is not None else DEFAULT_MODEL_VETO_BUFFER,
    }


def _monitor_thresholds_for_side(monitor: dict[str, Any], side: str) -> tuple[float, float]:
    normalized = side.upper()
    if normalized == "YES":
        return (
            _to_float(monitor.get("yes_take_profit_pct"), DEFAULT_YES_TAKE_PROFIT_PCT),
            _to_float(monitor.get("yes_stop_loss_pct"), DEFAULT_YES_STOP_LOSS_PCT),
        )
    if normalized == "NO":
        return (
            _to_float(monitor.get("no_take_profit_pct"), DEFAULT_NO_TAKE_PROFIT_PCT),
            _to_float(monitor.get("no_stop_loss_pct"), DEFAULT_NO_STOP_LOSS_PCT),
        )
    return (
        _to_float(monitor.get("take_profit_pct"), DEFAULT_TAKE_PROFIT_PCT),
        _to_float(monitor.get("stop_loss_pct"), DEFAULT_STOP_LOSS_PCT),
    )


def _settlement_first_no_min_cost_for_row(row: sqlite3.Row) -> float | None:
    profile = _row_risk_profile(row)
    if profile is not None and normalize_risk_profile_name(profile) == "research":
        return DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST
    return None


def _position_mark_status(
    unrealized_pnl: float | None,
    unrealized_roi: float | None,
    monitor: dict[str, Any],
    side: str,
) -> dict[str, str]:
    if unrealized_pnl is None or unrealized_roi is None:
        return {
            "status": "MARK_PENDING",
            "label": "Mark pending",
            "tone": "warn",
            "monitor_action": "WAITING_FOR_MARK",
        }

    take_profit_pct, stop_loss_pct = _monitor_thresholds_for_side(monitor, side)
    take_profit = take_profit_pct / 100.0
    stop_loss = stop_loss_pct / 100.0
    if unrealized_roi >= take_profit:
        monitor_action = "TAKE_PROFIT_READY"
    elif unrealized_roi <= -stop_loss:
        monitor_action = "STOP_LOSS_READY"
    else:
        monitor_action = "HOLD"

    if unrealized_pnl > 0.005:
        return {
            "status": "WINNING",
            "label": "Winning",
            "tone": "good",
            "monitor_action": monitor_action,
        }
    if unrealized_pnl < -0.005:
        return {
            "status": "LOSING",
            "label": "Losing",
            "tone": "bad",
            "monitor_action": monitor_action,
        }
    return {
        "status": "FLAT",
        "label": "Flat",
        "tone": "warn",
        "monitor_action": monitor_action,
    }


def _paper_row(
    row: sqlite3.Row,
    mark: dict[str, Any] | None = None,
    monitor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons = _json_list(row["reasons_json"])
    outcome_diagnostics = _json_object(_sqlite_row_value(row, "outcome_diagnostics_json"))
    outcome = outcome_diagnostics.get("outcome") if isinstance(outcome_diagnostics, dict) else {}
    monitor = monitor or _paper_monitor_config()
    side = _side_from_row(row)
    contracts = _to_float(row["contracts"])
    entry_price = row["entry_price"] if row["entry_price"] is not None else row["yes_ask"]
    cost_per_contract = _to_float(row["cost_per_contract"])
    fee_per_contract = _to_float(row["fee_per_contract"])
    risk = contracts * cost_per_contract
    current_bid = _to_float(mark.get("bid"), None) if mark else None
    current_ask = _to_float(mark.get("ask"), None) if mark else None
    # Same fee schedule the monitor applies (profile rates + series-specific
    # rounding), so displayed thresholds match executable ones (audit UI-01).
    fee_config = strategy_config_for_profile(
        str(_sqlite_row_value(row, "risk_profile") or "live")
    )
    exit_fee_kwargs = {
        "series_ticker": str(row["market_ticker"]),
        "fee_multiplier": fee_config.fee_multiplier,
        "taker_rate": fee_config.taker_fee_rate,
        "maker_rate": fee_config.maker_fee_rate,
    }
    current_exit_fee = (
        quadratic_fee_average_per_contract(current_bid, contracts, **exit_fee_kwargs)
        if current_bid is not None and current_bid > 0
        else None
    )
    current_net_exit = (
        current_bid - current_exit_fee
        if current_bid is not None and current_exit_fee is not None
        else None
    )
    current_value = (
        contracts * current_net_exit
        if current_net_exit is not None
        else None
    )
    unrealized_pnl = (
        current_value - risk
        if current_value is not None
        else None
    )
    unrealized_roi = (
        unrealized_pnl / risk
        if unrealized_pnl is not None and risk > 0
        else None
    )
    take_profit_pct, stop_loss_pct = _monitor_thresholds_for_side(monitor, side)
    take_profit = take_profit_pct / 100.0
    stop_loss = stop_loss_pct / 100.0
    legacy_take_profit_net = cost_per_contract * (1.0 + take_profit)
    stop_loss_net = max(0.0, cost_per_contract * (1.0 - stop_loss))
    model_side_probability = _to_float(mark.get("model_side_probability"), None) if mark else None
    model_take_profit_net = convergence_take_profit_net(model_side_probability)
    if model_take_profit_net is not None:
        take_profit_net = model_take_profit_net
        take_profit_basis = "model_fair_value"
    else:
        take_profit_net = legacy_take_profit_net
        take_profit_basis = "legacy_percent"
    mark_status = _position_mark_status(unrealized_pnl, unrealized_roi, monitor, side)
    exit_rule_reason = None
    if current_net_exit is not None:
        signal = decide_exit(
            side=side,
            entry_cost=cost_per_contract,
            net_exit=current_net_exit,
            stop_loss_net=stop_loss_net,
            model_side_probability=model_side_probability,
            model_veto_buffer=_to_float(monitor.get("model_veto_buffer"), DEFAULT_MODEL_VETO_BUFFER),
            model_veto_max_loss_roi=(
                _to_float(
                    monitor.get("model_veto_max_loss_pct"),
                    DEFAULT_MODEL_VETO_MAX_LOSS_PCT,
                )
                / 100.0
            ),
            legacy_take_profit_net=legacy_take_profit_net,
            stop_loss_pct=stop_loss_pct,
            settlement_first_no_min_cost=_settlement_first_no_min_cost_for_row(row),
        )
        exit_rule_reason = signal.reason
        mark_status = {**mark_status, "monitor_action": signal.action}
    if row["status"] == "PAPER_LIMIT_RESTING":
        mark_status = {
            "status": "LIMIT_RESTING",
            "label": "Limit pending",
            "tone": "warn",
            "monitor_action": "LIMIT_RESTING",
        }
    elif row["status"] in {"PAPER_SETTLED", "PAPER_CLOSED"}:
        realized = _to_float(row["realized_pnl"], 0.0)
        mark_status = {
            "status": "RESOLVED",
            "label": "Resolved",
            "tone": "good" if realized > 0 else "bad" if realized < 0 else "warn",
            "monitor_action": "RESOLVED",
        }
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "filled_at": _sqlite_row_value(row, "filled_at"),
        "cancelled_at": _sqlite_row_value(row, "cancelled_at"),
        "expires_at": _sqlite_row_value(row, "expires_at"),
        "account_id": _sqlite_row_value(row, "account_id"),
        "strategy_fingerprint": (
            _sqlite_row_value(row, "strategy_fingerprint") or "legacy_independent_sizing"
        ),
        "sleeve": _sqlite_row_value(row, "sleeve"),
        "fill_model": _sqlite_row_value(row, "fill_model"),
        "target_date": row["target_date"],
        "ticker": row["market_ticker"],
        "label": row["label"],
        "side": side,
        "status": row["status"],
        "risk_profile": _row_risk_profile(row),
        "diagnostics_available": bool(
            _sqlite_row_value(row, "diagnostics_json")
            or _sqlite_row_value(row, "outcome_diagnostics_json")
        ),
        "entry_decision_snapshot_id": _sqlite_row_value(row, "entry_decision_snapshot_id"),
        "contracts": _round(contracts, 4),
        "entry_mode": row["entry_mode"] if "entry_mode" in row.keys() else "market",
        "limit_price": _round(row["limit_price"], 4) if "limit_price" in row.keys() else None,
        "limit_fee_per_contract": (
            _round(row["limit_fee_per_contract"], 4)
            if "limit_fee_per_contract" in row.keys()
            else None
        ),
        "limit_cost_per_contract": (
            _round(row["limit_cost_per_contract"], 4)
            if "limit_cost_per_contract" in row.keys()
            else None
        ),
        "limit_edge": _round(row["limit_edge"], 4) if "limit_edge" in row.keys() else None,
        "limit_edge_lcb": (
            _round(row["limit_edge_lcb"], 4)
            if "limit_edge_lcb" in row.keys()
            else None
        ),
        "entry_price": _round(entry_price, 4),
        "entry_fee_per_contract": _round(fee_per_contract, 4),
        "cost_per_contract": _round(cost_per_contract, 4),
        "initial_cost": _round(risk, 2),
        "risk": _round(risk, 2),
        "max_profit": _round(contracts * max(0.0, 1.0 - cost_per_contract), 2),
        "max_loss": _round(risk, 2),
        "probability": _round(row["probability"], 4),
        "probability_lcb": _round(row["probability_lcb"], 4),
        "edge": _round(row["edge"], 4),
        "edge_lcb": _round(row["edge_lcb"], 4),
        "quality_score": _round(row["trade_quality_score"], 1),
        "expected_profit": _round(row["expected_profit"], 2),
        "current_bid": _round(current_bid, 4),
        "current_ask": _round(current_ask, 4),
        "current_spread": _round(mark.get("spread"), 4) if mark else None,
        "current_market_probability": _round(mark.get("market_probability"), 4) if mark else None,
        "current_snapshot_at": mark.get("created_at") if mark else None,
        "current_source": mark.get("source") if mark else None,
        "model_side_probability": _round(model_side_probability, 4),
        "current_exit_fee_per_contract": _round(current_exit_fee, 4),
        "current_net_exit": _round(current_net_exit, 4),
        "current_value": _round(current_value, 2),
        "unrealized_pnl": _round(unrealized_pnl, 2),
        "unrealized_roi": _round(unrealized_roi, 4),
        "position_status": mark_status["status"],
        "position_status_label": mark_status["label"],
        "position_status_tone": mark_status["tone"],
        "monitor_action": mark_status["monitor_action"],
        "take_profit_pct": _round(take_profit_pct, 2),
        "stop_loss_pct": _round(stop_loss_pct, 2),
        "take_profit_basis": take_profit_basis,
        "stop_loss_basis": "legacy_percent_floor",
        "exit_rule_reason": exit_rule_reason,
        "take_profit_pnl": _round(risk * take_profit, 2),
        "stop_loss_pnl": _round(-(risk * stop_loss), 2),
        "take_profit_net_exit": _round(take_profit_net, 4),
        "stop_loss_net_exit": _round(stop_loss_net, 4),
        "take_profit_bid": exit_bid_for_net(
            take_profit_net, contracts, **exit_fee_kwargs
        ),
        "stop_loss_bid": exit_bid_for_net(stop_loss_net, contracts, **exit_fee_kwargs),
        "settlement_high_f": _round(row["settlement_high_f"], 1),
        "resolved_yes": row["resolved_yes"],
        "exit_price": _round(row["exit_price"], 4),
        "exit_fee_per_contract": _round(row["exit_fee_per_contract"], 4),
        "realized_pnl": _round(row["realized_pnl"], 2),
        "realized_roi": (
            _round(_to_float(row["realized_pnl"]) / risk, 4)
            if row["realized_pnl"] is not None and risk > 0
            else None
        ),
        "closed_at": row["closed_at"],
        "settled_at": row["settled_at"],
        "outcome_reason": outcome.get("win_loss_reason") if isinstance(outcome, dict) else None,
        "forecast_error_f": (
            _round(outcome.get("forecast_error_f"), 2)
            if isinstance(outcome, dict)
            else None
        ),
        "reasons": reasons,
        "why_good": _why_trade_good(row, reasons),
    }


def _paper_action_row(row: sqlite3.Row) -> dict[str, Any]:
    action_time = row["closed_at"] or row["settled_at"] or row["created_at"]
    contracts = _to_float(row["contracts"])
    risk = contracts * _to_float(row["cost_per_contract"])
    return {
        "id": row["id"],
        "time": action_time,
        "ticker": row["market_ticker"],
        "label": row["label"],
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": _round(contracts, 4),
        "status": row["status"],
        "entry_price": _round(row["entry_price"] if row["entry_price"] is not None else row["yes_ask"], 4),
        "cost_per_contract": _round(row["cost_per_contract"], 4),
        "initial_cost": _round(risk, 2),
        "exit_price": _round(row["exit_price"], 4),
        "exit_fee_per_contract": _round(row["exit_fee_per_contract"], 4),
        "settlement_high_f": _round(row["settlement_high_f"], 1),
        "realized_pnl": _round(row["realized_pnl"], 2),
        "realized_roi": (
            _round(_to_float(row["realized_pnl"]) / risk, 4)
            if row["realized_pnl"] is not None and risk > 0
            else None
        ),
        "note": "closed by monitor" if row["closed_at"] else "settled against official high",
    }


def _paper_open_action_row(row: sqlite3.Row) -> dict[str, Any]:
    contracts = _to_float(row["contracts"])
    risk = contracts * _to_float(row["cost_per_contract"])
    return {
        "id": row["id"],
        "time": row["created_at"],
        "ticker": row["market_ticker"],
        "label": row["label"],
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": _round(contracts, 4),
        "status": "OPEN",
        "entry_price": _round(
            row["entry_price"] if row["entry_price"] is not None else row["yes_ask"],
            4,
        ),
        "cost_per_contract": _round(row["cost_per_contract"], 4),
        "initial_cost": _round(risk, 2),
        "exit_price": None,
        "exit_fee_per_contract": None,
        "settlement_high_f": None,
        "realized_pnl": None,
        "realized_roi": None,
        "note": "paper order opened",
    }


def _paper_limit_action_row(row: sqlite3.Row) -> dict[str, Any]:
    contracts = _to_float(row["contracts"])
    risk = contracts * _to_float(row["cost_per_contract"])
    return {
        "id": row["id"],
        "time": row["created_at"],
        "ticker": row["market_ticker"],
        "label": row["label"],
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": _round(contracts, 4),
        "status": "LIMIT_RESTING",
        "entry_price": _round(
            row["entry_price"] if row["entry_price"] is not None else row["yes_ask"],
            4,
        ),
        "cost_per_contract": _round(row["cost_per_contract"], 4),
        "initial_cost": _round(risk, 2),
        "exit_price": None,
        "exit_fee_per_contract": None,
        "settlement_high_f": None,
        "realized_pnl": None,
        "realized_roi": None,
        "note": "paper buy-limit resting until market ask trades at or below limit",
    }


def _paper_monitor_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        label = row["label"] or row["market_ticker"]
    except (IndexError, KeyError):
        label = row["market_ticker"]
    return {
        "id": row["order_id"],
        "time": row["created_at"],
        "ticker": row["market_ticker"],
        "label": label,
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": None,
        "status": row["action"],
        "entry_price": None,
        "cost_per_contract": None,
        "initial_cost": None,
        "exit_price": _round(row["live_bid"], 4),
        "exit_fee_per_contract": _round(row["exit_fee_per_contract"], 4),
        "settlement_high_f": None,
        # These are an OPEN position's live mark, not a realized close: live_bid
        # is the current sell bid and unrealized_pnl is mark-to-market. The
        # `unrealized` flag lets the UI label them as such so a HOLD inspection
        # does not read as a closed trade (see Finding 5 in
        # docs/trading_engine_diagnosis_2026-06-16.md).
        "realized_pnl": _round(row["unrealized_pnl"], 2),
        "realized_roi": _round(row["unrealized_roi"], 4),
        "unrealized": True,
        "note": row["reason"] or "monitor inspection (open position)",
    }


def _duplicate_group_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "target_date": str(row["target_date"]),
        "risk_profile": str(row["risk_profile"]),
        "ticker": row["market_ticker"],
        "side": row["side"],
        "open_orders": int(row["open_orders"]),
    }
