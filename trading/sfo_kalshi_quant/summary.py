from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import SFO_TZ, StrategyConfig
from .forecast import ForecastDataError, SfoForecasterAdapter


def build_paper_summary(
    *,
    db_path: Path,
    forecaster_root: Path,
    config: StrategyConfig | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the daily + rolling N-day paper-trading summary.

    All P&L is attributed to the local SFO calendar day the order was closed or
    settled; entries are attributed to the day they were opened. Forecast error
    uses only clean next-day blend rows, so the accuracy series has no
    look-ahead from same-day observed-high locks.
    """

    cfg = config or StrategyConfig()
    local_now = (now or datetime.now(UTC)).astimezone(SFO_TZ)
    today = local_now.date()
    window_start = today - timedelta(days=days - 1)
    day_keys = [(window_start + timedelta(days=offset)).isoformat() for offset in range(days)]

    orders = _load_orders(db_path)
    decision_stats = _decision_stats(db_path, window_start)
    forecast_errors = _forecast_error_by_date(forecaster_root, window_start, today)

    per_day = {key: _empty_day(key) for key in day_keys}
    realized_before_window = 0.0
    total_realized_all_time = 0.0

    for order in orders:
        opened_day = _local_day(order["created_at"])
        resolved_at = order["closed_at"] or order["settled_at"]
        resolved_day = _local_day(resolved_at) if resolved_at else None
        pnl = order["realized_pnl"]
        spend = order["contracts"] * order["cost_per_contract"]

        if pnl is not None:
            total_realized_all_time += pnl
            if resolved_day is not None and resolved_day < window_start.isoformat():
                realized_before_window += pnl

        profile = order["risk_profile"]
        if opened_day in per_day:
            day = per_day[opened_day]
            day["opened"] += 1
            day["opened_spend"] += spend
            day_profile = _day_profile(day, profile)
            day_profile["opened"] += 1
            day_profile["opened_spend"] += spend
        # PAPER_EXPIRED rows are resting limits that never filled (realized_pnl
        # 0.0); they resolved no position, so they must not inflate the settled
        # count or the hit-rate denominator as phantom non-wins.
        if (
            resolved_day in per_day
            and pnl is not None
            and order["status"] != "PAPER_EXPIRED"
        ):
            day = per_day[resolved_day]
            day["realized_pnl"] += pnl
            day["resolved_spend"] += spend
            day_profile = _day_profile(day, profile)
            day_profile["realized_pnl"] += pnl
            day_profile["resolved"] += 1
            day_profile["resolved_spend"] += spend
            if order["closed_at"]:
                day["closed"] += 1
                day_profile["closed"] += 1
            else:
                day["settled"] += 1
                day_profile["settled"] += 1
            if pnl > 0:
                day["wins"] += 1
                day_profile["wins"] += 1
            elif pnl < 0:
                day["losses"] += 1
                day_profile["losses"] += 1

    open_orders = [order for order in orders if order["realized_pnl"] is None and order["status"] == "PAPER_FILLED"]
    open_risk = sum(order["contracts"] * order["cost_per_contract"] for order in open_orders)

    cumulative = realized_before_window
    days_out: list[dict[str, Any]] = []
    for key in day_keys:
        day = per_day[key]
        opening_equity = cfg.paper_bankroll + cumulative
        cumulative += day["realized_pnl"]
        resolved = day["closed"] + day["settled"]
        day["hit_rate"] = day["wins"] / resolved if resolved else None
        day["roi"] = day["realized_pnl"] / day["resolved_spend"] if day["resolved_spend"] > 0 else None
        day["cumulative_realized"] = round(cumulative, 2)
        day["opening_equity"] = round(opening_equity, 2)
        day["daily_realized_pnl"] = round(day["realized_pnl"], 2)
        day["closing_equity"] = round(cfg.paper_bankroll + cumulative, 2)
        day["realized_pnl"] = round(day["realized_pnl"], 2)
        day["opened_spend"] = round(day["opened_spend"], 2)
        day["resolved_spend"] = round(day["resolved_spend"], 2)
        stats = decision_stats["per_day"].get(key, {})
        for name, profile_stats in (stats.get("profiles") or {}).items():
            day_profile = _day_profile(day, name)
            day_profile["signals"] = profile_stats.get("signals", 0)
            day_profile["approved_signals"] = profile_stats.get("approved", 0)
        for profile_stats in day["profiles"].values():
            profile_resolved = int(profile_stats["closed"] + profile_stats["settled"])
            profile_stats["hit_rate"] = (
                profile_stats["wins"] / profile_resolved if profile_resolved else None
            )
            profile_stats["roi"] = (
                profile_stats["realized_pnl"] / profile_stats["resolved_spend"]
                if profile_stats["resolved_spend"] > 0
                else None
            )
            profile_stats["realized_pnl"] = round(profile_stats["realized_pnl"], 2)
            profile_stats["opened_spend"] = round(profile_stats["opened_spend"], 2)
            profile_stats["resolved_spend"] = round(profile_stats["resolved_spend"], 2)
        day["signals"] = stats.get("signals", 0)
        day["approved_signals"] = stats.get("approved", 0)
        day["avg_model_probability"] = stats.get("avg_model_probability")
        day["avg_market_probability"] = stats.get("avg_market_probability")
        forecast = forecast_errors.get(key)
        day["forecast_predicted_high_f"] = forecast["predicted"] if forecast else None
        day["forecast_actual_high_f"] = forecast["actual"] if forecast else None
        day["forecast_error_f"] = forecast["error"] if forecast else None
        days_out.append(day)

    window_orders = [
        order
        for order in orders
        if order["realized_pnl"] is not None
        and order["status"] in {"PAPER_SETTLED", "PAPER_CLOSED"}
        and (resolved := order["closed_at"] or order["settled_at"]) is not None
        and _local_day(resolved) >= window_start.isoformat()
    ]
    window_pnl = sum(order["realized_pnl"] for order in window_orders)
    window_spend = sum(order["contracts"] * order["cost_per_contract"] for order in window_orders)
    window_wins = sum(1 for order in window_orders if order["realized_pnl"] > 0)
    window_losses = sum(1 for order in window_orders if order["realized_pnl"] < 0)
    scanning_profiles = [
        row["risk_profile"]
        for row in (decision_stats["gate_behavior"].get("by_profile") or [])
    ]
    window_profiles = _window_profile_totals(window_orders, open_orders, scanning_profiles)
    forecast_abs_errors = [row["error"] for row in forecast_errors.values() if row["error"] is not None]

    ranked = sorted(window_orders, key=lambda order: order["realized_pnl"], reverse=True)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": days,
        "window_start": window_start.isoformat(),
        "window_end": today.isoformat(),
        # `bankroll` is the static STARTING notional (kept for backward compat
        # and aliased as starting_bankroll). `current_equity` is the honest live
        # number: starting notional + all-time realized PnL. The dashboard should
        # lead with current_equity so a static $1000 is never mistaken for the
        # live book value.
        "bankroll": round(cfg.paper_bankroll, 2),
        "starting_bankroll": round(cfg.paper_bankroll, 2),
        "current_equity": round(cfg.paper_bankroll + total_realized_all_time, 2),
        "days": days_out,
        "totals": {
            "trades_opened": sum(day["opened"] for day in days_out),
            "trades_closed": sum(day["closed"] for day in days_out),
            "trades_settled": sum(day["settled"] for day in days_out),
            "open_positions": len(open_orders),
            "open_risk": round(open_risk, 2),
            "realized_pnl": round(window_pnl, 2),
            "cumulative_realized_pnl": round(total_realized_all_time, 2),
            "capital_resolved": round(window_spend, 2),
            "roi": round(window_pnl / window_spend, 4) if window_spend > 0 else None,
            "wins": window_wins,
            "losses": window_losses,
            "hit_rate": round(window_wins / (window_wins + window_losses), 4)
            if (window_wins + window_losses)
            else None,
            "mean_abs_forecast_error_f": round(sum(forecast_abs_errors) / len(forecast_abs_errors), 2)
            if forecast_abs_errors
            else None,
        },
        "profiles": window_profiles,
        "side_performance": _side_performance(window_orders),
        "exit_reasons": _exit_reason_breakdown(window_orders),
        # Per-profile views of the same two breakdowns, keyed by risk_profile, so
        # selecting a profile tab on the dashboard shows that profile's YES/NO
        # split and exit-reason mix instead of the aggregate-only cards going
        # blank. Orders with no risk_profile carry the "unknown" sentinel
        # (_load_orders) and are aggregate-only -- excluded here so the published
        # maps never carry an "unknown" key (consistent with _profile_names).
        "side_performance_by_profile": {
            name: _side_performance([o for o in window_orders if o["risk_profile"] == name])
            for name in _profile_keys(window_orders)
        },
        "exit_reasons_by_profile": {
            name: _exit_reason_breakdown([o for o in window_orders if o["risk_profile"] == name])
            for name in _profile_keys(window_orders)
        },
        "biggest_winners": [_order_brief(order) for order in ranked[:3] if order["realized_pnl"] > 0],
        "biggest_losers": [
            _order_brief(order) for order in sorted(window_orders, key=lambda order: order["realized_pnl"])[:3]
            if order["realized_pnl"] < 0
        ],
        "gate_behavior": decision_stats["gate_behavior"],
        "model_vs_market": decision_stats["model_vs_market"],
        "data_collected": _data_collected(db_path, window_start),
        "learnings": _learnings(window_orders, decision_stats, forecast_abs_errors),
    }
    payload["recommended_changes"] = _recommended_changes(payload)
    return payload


def write_paper_summary(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_paper_summary_csv(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "date",
        "opened",
        "closed",
        "settled",
        "wins",
        "losses",
        "hit_rate",
        "realized_pnl",
        "cumulative_realized",
        "opened_spend",
        "resolved_spend",
        "roi",
        "signals",
        "approved_signals",
        "avg_model_probability",
        "avg_market_probability",
        "forecast_predicted_high_f",
        "forecast_actual_high_f",
        "forecast_error_f",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for day in payload.get("days", []):
            writer.writerow(day)


def _empty_day(key: str) -> dict[str, Any]:
    return {
        "date": key,
        "opened": 0,
        "closed": 0,
        "settled": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "opened_spend": 0.0,
        "resolved_spend": 0.0,
        "profiles": {},
    }


def _day_profile(day: dict[str, Any], profile: str) -> dict[str, Any]:
    return day["profiles"].setdefault(
        profile,
        {
            "opened": 0,
            "closed": 0,
            "settled": 0,
            "resolved": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl": 0.0,
            "opened_spend": 0.0,
            "resolved_spend": 0.0,
            "signals": 0,
            "approved_signals": 0,
            "hit_rate": None,
            "roi": None,
        },
    )


def _profile_keys(window_orders: list) -> set[str]:
    """Real risk-profile keys present in the orders, excluding the "unknown"
    sentinel (_load_orders assigns "unknown" to profile-less orders, which are
    aggregate-only and never surfaced as a profile tab)."""

    return {
        order["risk_profile"]
        for order in window_orders
        if order["risk_profile"] and order["risk_profile"] != "unknown"
    }


def _exit_reason_breakdown(window_orders: list) -> dict[str, int]:
    """How positions left the book: held to settlement vs early take-profit vs
    early stop-loss vs never-filled expiration.

    The headline diagnostic for the exit fix -- before it, the unreachable
    %-of-cost take-profit meant favorites only ever 'held_to_settlement'.
    PAPER_CLOSED is split by realized PnL sign as a proxy for take-profit vs
    stop-loss without joining the monitor snapshots. A break-even close
    (realized_pnl == 0) is its own bucket rather than silently counted as a
    take-profit, so this view agrees with the resolved_yes-based win/loss
    classification in db.py (a break-even close is undecided, not a profit).
    """

    counts = {
        "held_to_settlement": 0,
        "closed_take_profit": 0,
        "closed_stop_loss": 0,
        "closed_break_even": 0,
        "expired_unfilled": 0,
    }
    for order in window_orders:
        status = order["status"]
        if status == "PAPER_EXPIRED":
            counts["expired_unfilled"] += 1
        elif status == "PAPER_CLOSED":
            pnl = float(order["realized_pnl"])
            if pnl > 0:
                counts["closed_take_profit"] += 1
            elif pnl < 0:
                counts["closed_stop_loss"] += 1
            else:
                counts["closed_break_even"] += 1
        elif status == "PAPER_SETTLED":
            counts["held_to_settlement"] += 1
    return counts


def _side_performance(window_orders: list) -> dict[str, dict[str, Any]]:
    """Per-side (YES vs NO) realized performance over the window.

    Surfaces the structural YES-vs-NO gap directly on the dashboard (live: the
    book is profitable on NO favorites and underwater on YES longshots). Excludes
    never-filled expirations, and uses wins/(wins+losses) so a break-even close
    does not distort the hit rate.
    """

    sides: dict[str, dict[str, Any]] = {
        side: {"trades": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0, "capital": 0.0}
        for side in ("YES", "NO")
    }
    for order in window_orders:
        if order["status"] == "PAPER_EXPIRED":
            continue
        side = str(order["side"] or "YES").upper()
        if side not in sides:
            continue
        bucket = sides[side]
        pnl = float(order["realized_pnl"])
        bucket["trades"] += 1
        bucket["realized_pnl"] += pnl
        bucket["capital"] += float(order["contracts"]) * float(order["cost_per_contract"])
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    for bucket in sides.values():
        decided = bucket["wins"] + bucket["losses"]
        bucket["hit_rate"] = round(bucket["wins"] / decided, 4) if decided else None
        bucket["roi"] = (
            round(bucket["realized_pnl"] / bucket["capital"], 4) if bucket["capital"] > 0 else None
        )
        bucket["realized_pnl"] = round(bucket["realized_pnl"], 2)
        bucket["capital"] = round(bucket["capital"], 2)
    return sides


def _window_profile_totals(
    window_orders: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    scanning_profiles: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-risk-profile window totals; the scan can run several profiles
    against one paper book, so blended totals hide which gate set is losing."""

    profiles: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        return profiles.setdefault(
            name,
            {
                "risk_profile": name,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "realized_pnl": 0.0,
                "capital_resolved": 0.0,
                "open_positions": 0,
                "open_risk": 0.0,
            },
        )

    for order in window_orders:
        stats = bucket(order["risk_profile"])
        stats["resolved"] += 1
        stats["realized_pnl"] += order["realized_pnl"]
        stats["capital_resolved"] += order["contracts"] * order["cost_per_contract"]
        if order["realized_pnl"] > 0:
            stats["wins"] += 1
        elif order["realized_pnl"] < 0:
            stats["losses"] += 1
    for order in open_orders:
        stats = bucket(order["risk_profile"])
        stats["open_positions"] += 1
        stats["open_risk"] += order["contracts"] * order["cost_per_contract"]
    # A profile that scans but never qualifies a trade must still show up:
    # an empty balanced row next to a losing research row is the diagnostic.
    for name in scanning_profiles or []:
        bucket(name)

    rows = []
    for name in sorted(profiles):
        stats = profiles[name]
        resolved = stats["resolved"]
        capital = stats["capital_resolved"]
        rows.append(
            {
                **stats,
                "realized_pnl": round(stats["realized_pnl"], 2),
                "capital_resolved": round(capital, 2),
                "open_risk": round(stats["open_risk"], 2),
                "hit_rate": round(stats["wins"] / resolved, 4) if resolved else None,
                "roi": round(stats["realized_pnl"] / capital, 4) if capital > 0 else None,
            }
        )
    return rows


def _row_value(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _load_orders(db_path: Path) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "paper_orders"):
            return []
        rows = conn.execute(
            "SELECT * FROM paper_orders WHERE status != 'REJECTED' ORDER BY created_at"
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "created_at": row["created_at"],
            "target_date": row["target_date"],
            "market_ticker": row["market_ticker"],
            "label": row["label"],
            "side": str(row["side"] or "YES").upper(),
            "risk_profile": _row_value(row, "risk_profile") or "unknown",
            "contracts": float(row["contracts"] or 0.0),
            "entry_price": float(row["entry_price"] if row["entry_price"] is not None else row["yes_ask"]),
            "cost_per_contract": float(row["cost_per_contract"] or 0.0),
            "probability": float(row["probability"] or 0.0),
            "edge_lcb": float(row["edge_lcb"] or 0.0),
            "trade_quality_score": float(row["trade_quality_score"] or 0.0),
            "status": row["status"],
            "closed_at": row["closed_at"],
            "settled_at": row["settled_at"],
            "realized_pnl": float(row["realized_pnl"]) if row["realized_pnl"] is not None else None,
        }
        for row in rows
    ]


def _decision_stats(db_path: Path, window_start: date) -> dict[str, Any]:
    empty = {
        "per_day": {},
        "gate_behavior": {
            "approved": 0,
            "rejected": 0,
            "top_rejections": [],
            "by_profile": [],
        },
        "model_vs_market": {},
    }
    if not Path(db_path).exists():
        return empty
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "decision_snapshots"):
            return empty
        columns = _table_columns(conn, "decision_snapshots")
        signal_approved_expr = (
            "signal_approved"
            if "signal_approved" in columns
            else "approved AS signal_approved"
        )
        entry_block_expr = (
            "entry_block_reason"
            if "entry_block_reason" in columns
            else "NULL AS entry_block_reason"
        )
        rows = conn.execute(
            f"""
            SELECT created_at, approved, {signal_approved_expr},
                   {entry_block_expr}, model_probability, market_probability,
                   reasons_json, COALESCE(risk_profile, 'unknown') AS risk_profile
            FROM decision_snapshots
            WHERE created_at >= ?
            """,
            (datetime.combine(window_start, datetime.min.time(), tzinfo=SFO_TZ).astimezone(UTC).isoformat(),),
        ).fetchall()

    per_day: dict[str, dict[str, Any]] = {}
    rejection_counts: dict[str, int] = {}
    rejection_counts_all: dict[str, int] = {}
    category_counts: dict[str, int] = {"no_data": 0, "edge": 0, "other": 0}
    approved_total = 0
    gaps: list[float] = []
    by_profile: dict[str, dict[str, Any]] = {}
    for row in rows:
        profile = str(row["risk_profile"])
        profile_stats = by_profile.setdefault(
            profile,
            {
                "signals": 0,
                "approved": 0,
                "rejection_counts": {},
                "rejection_counts_all": {},
                "rejection_categories": {"no_data": 0, "edge": 0, "other": 0},
                "entry_block_reasons": {},
            },
        )
        profile_stats["signals"] += 1
        signal_approved = bool(
            row["signal_approved"] if row["signal_approved"] is not None else row["approved"]
        )
        entry_block_reason = row["entry_block_reason"]
        if signal_approved:
            profile_stats["approved"] += 1
        day = _local_day(row["created_at"])
        stats = per_day.setdefault(
            day,
            {
                "signals": 0,
                "approved": 0,
                "model_p_sum": 0.0,
                "market_p_sum": 0.0,
                "p_count": 0,
                "profiles": {},
            },
        )
        day_profile = stats["profiles"].setdefault(
            profile,
            {"signals": 0, "approved": 0},
        )
        day_profile["signals"] += 1
        stats["signals"] += 1
        if signal_approved:
            stats["approved"] += 1
            day_profile["approved"] += 1
            approved_total += 1
            if entry_block_reason:
                block_counts = profile_stats["entry_block_reasons"]
                block_counts[str(entry_block_reason)] = (
                    block_counts.get(str(entry_block_reason), 0) + 1
                )
        else:
            reason = _primary_reason(row["reasons_json"])
            if reason:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                profile_counts = profile_stats["rejection_counts"]
                profile_counts[reason] = profile_counts.get(reason, 0) + 1
            # Tally every reason on the row, not just the first, so a gate that
            # is always appended after source-spread is still visible. Bucket
            # the row by its most fundamental blocker (no_data > edge) so the
            # dashboard can separate "correctly idle" from "rejected real edge".
            row_reasons = _all_reasons(row["reasons_json"])
            seen: set[str] = set()
            row_category = "other"
            for normalized in row_reasons:
                if normalized not in seen:
                    rejection_counts_all[normalized] = rejection_counts_all.get(normalized, 0) + 1
                    profile_all = profile_stats["rejection_counts_all"]
                    profile_all[normalized] = profile_all.get(normalized, 0) + 1
                    seen.add(normalized)
                category = _reason_category(normalized)
                if category == "no_data" or (category == "edge" and row_category != "no_data"):
                    row_category = category
            category_counts[row_category] = category_counts.get(row_category, 0) + 1
            profile_categories = profile_stats["rejection_categories"]
            profile_categories[row_category] = profile_categories.get(row_category, 0) + 1
        model_p = row["model_probability"]
        market_p = row["market_probability"]
        if model_p is not None and market_p is not None:
            stats["model_p_sum"] += float(model_p)
            stats["market_p_sum"] += float(market_p)
            stats["p_count"] += 1
            gaps.append(abs(float(model_p) - float(market_p)))

    for stats in per_day.values():
        count = stats.pop("p_count")
        model_sum = stats.pop("model_p_sum")
        market_sum = stats.pop("market_p_sum")
        stats["avg_model_probability"] = round(model_sum / count, 4) if count else None
        stats["avg_market_probability"] = round(market_sum / count, 4) if count else None

    top_rejections = sorted(rejection_counts.items(), key=lambda item: item[1], reverse=True)[:6]
    top_rejections_all = sorted(
        rejection_counts_all.items(), key=lambda item: item[1], reverse=True
    )[:8]
    return {
        "per_day": per_day,
        "gate_behavior": {
            "approved": approved_total,
            "rejected": sum(rejection_counts.values()),
            "top_rejections": [
                {"reason": reason, "count": count} for reason, count in top_rejections
            ],
            # Every gate that fired (a row can trip several), so a gate appended
            # after source-spread is not hidden by the primary-reason count.
            "top_rejections_all": [
                {"reason": reason, "count": count} for reason, count in top_rejections_all
            ],
            # Rows bucketed by their most fundamental blocker: no_data means the
            # engine was correctly idle (bad inputs); edge means a live market
            # failed a price/edge gate. This is the number to read before
            # loosening any gate to "trade more".
            "rejection_categories": dict(category_counts),
            "by_profile": [
                _profile_gate_stats(name, stats)
                for name, stats in sorted(by_profile.items())
            ],
        },
        "model_vs_market": {
            "samples": len(gaps),
            "mean_abs_gap": round(sum(gaps) / len(gaps), 4) if gaps else None,
            "max_abs_gap": round(max(gaps), 4) if gaps else None,
        },
    }


def _profile_gate_stats(name: str, stats: dict[str, Any]) -> dict[str, Any]:
    rejection_counts = stats.get("rejection_counts") or {}
    rejection_counts_all = stats.get("rejection_counts_all") or {}
    entry_block_reasons = stats.get("entry_block_reasons") or {}
    return {
        "risk_profile": name,
        "signals": int(stats.get("signals") or 0),
        "approved": int(stats.get("approved") or 0),
        "top_rejections": [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                rejection_counts.items(), key=lambda item: item[1], reverse=True
            )[:6]
        ],
        "top_rejections_all": [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                rejection_counts_all.items(), key=lambda item: item[1], reverse=True
            )[:8]
        ],
        "rejection_categories": dict(stats.get("rejection_categories") or {}),
        "entry_block_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                entry_block_reasons.items(), key=lambda item: item[1], reverse=True
            )[:6]
        ],
    }


def _forecast_error_by_date(
    forecaster_root: Path,
    window_start: date,
    window_end: date,
) -> dict[str, dict[str, float | None]]:
    adapter = SfoForecasterAdapter(Path(forecaster_root))
    try:
        outcomes = adapter.load_clean_blend_outcomes()
    except (ForecastDataError, OSError, ValueError):
        return {}
    output: dict[str, dict[str, float | None]] = {}
    for outcome in outcomes:
        if window_start <= outcome.local_date <= window_end:
            output[outcome.local_date.isoformat()] = {
                "predicted": round(outcome.predicted_high_f, 2),
                "actual": round(outcome.actual_high_f, 1),
                "error": round(abs(outcome.actual_high_f - outcome.predicted_high_f), 2),
            }
    return output


def _data_collected(db_path: Path, window_start: date) -> dict[str, int]:
    tables = (
        "decision_snapshots",
        "probability_snapshots",
        "forecast_snapshots",
        "market_snapshots",
        "paper_monitor_snapshots",
        "paper_orders",
    )
    counts: dict[str, int] = {}
    if not Path(db_path).exists():
        return {table: 0 for table in tables}
    cutoff = datetime.combine(window_start, datetime.min.time(), tzinfo=SFO_TZ).astimezone(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        for table in tables:
            if not _table_exists(conn, table):
                counts[table] = 0
                continue
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()
            counts[table] = int(row[0] or 0)
    return counts


def _learnings(
    window_orders: list[dict[str, Any]],
    decision_stats: dict[str, Any],
    forecast_abs_errors: list[float],
) -> list[str]:
    notes: list[str] = []
    resolved = [order for order in window_orders if order["realized_pnl"] is not None]
    if resolved:
        cheap = [order for order in resolved if order["entry_price"] <= 0.05]
        if cheap:
            cheap_wins = sum(1 for order in cheap if order["realized_pnl"] > 0)
            cheap_p = sum(order["probability"] for order in cheap) / len(cheap)
            notes.append(
                f"Sub-5c entries: {cheap_wins}/{len(cheap)} won vs an average modeled "
                f"probability of {cheap_p:.1%}. Tail probabilities remain the main "
                "calibration risk; the edge_lcb >= 0 gate exists to filter these."
            )
        negative_lcb = [order for order in resolved if order["edge_lcb"] < 0]
        positive_lcb = [order for order in resolved if order["edge_lcb"] >= 0]
        if negative_lcb and positive_lcb:
            notes.append(
                f"Lower-bound edge split: edge_lcb<0 trades netted "
                f"${sum(order['realized_pnl'] for order in negative_lcb):+.2f} "
                f"({len(negative_lcb)} trades) vs edge_lcb>=0 at "
                f"${sum(order['realized_pnl'] for order in positive_lcb):+.2f} "
                f"({len(positive_lcb)} trades)."
            )
        by_side: dict[str, list[float]] = {}
        for order in resolved:
            by_side.setdefault(order["side"], []).append(order["realized_pnl"])
        for side, pnls in sorted(by_side.items()):
            wins = sum(1 for value in pnls if value > 0)
            notes.append(
                f"{side} side: {wins}/{len(pnls)} won, net ${sum(pnls):+.2f} this window."
            )
        by_profile: dict[str, list[float]] = {}
        for order in resolved:
            by_profile.setdefault(order["risk_profile"], []).append(order["realized_pnl"])
        if len(by_profile) > 1:
            parts = []
            for profile, pnls in sorted(by_profile.items()):
                wins = sum(1 for value in pnls if value > 0)
                parts.append(f"{profile} {wins}/{len(pnls)} won (${sum(pnls):+.2f})")
            notes.append(
                "Profile split: " + "; ".join(parts) + ". Judge each gate set on its "
                "own book; research-profile losses are expected data-collection cost."
            )
    gate = decision_stats.get("gate_behavior") or {}
    top = (gate.get("top_rejections") or [])[:2]
    if top:
        joined = "; ".join(f"{row['reason']} ({row['count']}x)" for row in top)
        notes.append(f"Most frequent gate rejections: {joined}.")
    if forecast_abs_errors:
        notes.append(
            f"Clean next-day forecast error averaged {sum(forecast_abs_errors) / len(forecast_abs_errors):.2f}F "
            f"across {len(forecast_abs_errors)} settled day(s) in this window."
        )
    if not notes:
        notes.append("No resolved trades in this window yet; gates are collecting decision snapshots only.")
    return notes


def _recommended_changes(payload: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    totals = payload.get("totals") or {}
    hit_rate = totals.get("hit_rate")
    roi = totals.get("roi")
    resolved = (totals.get("wins") or 0) + (totals.get("losses") or 0)
    if resolved == 0:
        recommendations.append(
            "Keep collecting: no resolved trades this window. Do not loosen gates to force activity."
        )
    elif resolved < 15:
        recommendations.append(
            f"Sample is small ({resolved} resolved trades); treat window ROI as noise and "
            "wait for at least 15 resolved trades before changing gates."
        )
    if roi is not None and resolved >= 15:
        if roi < -0.05:
            recommendations.append(
                "Window ROI is materially negative on a fair sample; audit the largest losers "
                "for a shared failure mode before the next change."
            )
        elif roi > 0.05:
            recommendations.append(
                "Window ROI is positive; consider a one-notch size increase only after a second "
                "positive window."
            )
    if hit_rate is not None and resolved >= 15 and hit_rate < 0.4:
        recommendations.append(
            "Hit rate is below 40%; check whether losses cluster in one bin distance or side."
        )
    error = totals.get("mean_abs_forecast_error_f")
    if error is not None and error > 2.0:
        recommendations.append(
            f"Mean absolute forecast error is {error:.2f}F; review source weights and recent "
            "Google/NWS disagreement before trusting tail bins."
        )
    if not recommendations:
        recommendations.append("No rule-based change is indicated by this window; keep the current gates.")
    return recommendations


def _order_brief(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order["id"],
        "target_date": order["target_date"],
        "ticker": order["market_ticker"],
        "label": order["label"],
        "side": order["side"],
        "risk_profile": order["risk_profile"],
        "entry_price": round(order["entry_price"], 2),
        "contracts": round(order["contracts"], 2),
        "realized_pnl": round(order["realized_pnl"], 2),
        "quality_score": round(order["trade_quality_score"], 1),
        "edge_lcb": round(order["edge_lcb"], 3),
    }


def _primary_reason(reasons_json: object) -> str | None:
    try:
        payload = json.loads(str(reasons_json))
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(payload, list) and payload:
        reason = str(payload[0])
        # Collapse numeric details so identical gates group together.
        return _normalize_reason(reason)
    return None


# Reasons grouped by what is actually blocking the trade. "no_data" means the
# inputs are unusable (no market, sources disagree, single source), so the
# engine is correctly idle rather than rejecting a real edge; "edge" means a
# live, well-formed market failed an edge/price gate. Keeping these separate is
# what makes under-trading diagnosable (a high-spread day floods the counts with
# source-spread blocks that otherwise masquerade as edge rejections).
_NO_DATA_REASONS = frozenset(
    {
        "source spread",
        "single-source forecast",
        "market status",
        "no live market",
        "same-day entry disabled",
    }
)
_EDGE_REASONS = frozenset(
    {
        "edge_lcb",
        "lower-bound edge",
        "edge",
        "spread",
        "spread fraction",
        "posterior probability",
        "model/market gap",
        "bid size",
        "bid",
        "ask size",
        "no displayed entry liquidity",
        "1c/2c tail requires exceptional support",
        "cheap tail",
        "all-in cost",
        "no exit support",
        "risk sizing produced zero contracts",
    }
)


def _reason_category(reason: str) -> str:
    if reason in _NO_DATA_REASONS:
        return "no_data"
    if reason in _EDGE_REASONS:
        return "edge"
    return "other"


def _normalize_reason(reason: str) -> str:
    for marker in (
        "source spread",
        "single-source forecast",
        "edge_lcb",
        "lower-bound edge",
        "edge",
        "spread fraction",
        "spread",
        "posterior probability",
        "model/market gap",
        "bid size",
        "ask size",
        "no displayed entry liquidity",
        "bid",
        "1c/2c tail requires exceptional support",
        "cheap tail",
        "same-day entry disabled",
        "all-in cost",
        "market status",
        "no exit support",
        "risk sizing produced zero contracts",
    ):
        if marker in reason:
            return marker
    return reason[:48]


def _all_reasons(reasons_json: object) -> list[str]:
    try:
        payload = json.loads(str(reasons_json))
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return [_normalize_reason(str(item)) for item in payload if item]
    return []


def _local_day(timestamp: object) -> str:
    parsed = _parse_timestamp(timestamp)
    if parsed is None:
        return ""
    return parsed.astimezone(SFO_TZ).date().isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
