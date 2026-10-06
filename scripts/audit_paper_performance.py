#!/usr/bin/env python3
"""Audit a fresh exported paper journal without opening a production database.

Input is the operator's read-only export: captured_at plus paper.paper_accounts
and paper.paper_orders rows. This reports observed outcomes, never simulated
fills or a forecast of future profits. Keep the full export in ignored .local.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import random
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trading"))
from sfo_kalshi_quant.logical_positions import group_logical_positions

OBJECTIVE_TZ = ZoneInfo("America/Los_Angeles")


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("audit timestamps must include a timezone")
    return parsed.astimezone(OBJECTIVE_TZ)


def mean_interval(values: list[float], block: int) -> dict | None:
    """Exploratory circular block bootstrap; preserves zero and outage days."""
    if len(values) < max(2, block):
        return None
    rng = random.Random(20261005)
    draws = []
    for _ in range(4000):
        sample = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            sample.extend(values[(start + i) % len(values)] for i in range(block))
        draws.append(math.fsum(sample[:len(values)]) / len(values))
    draws.sort()
    return {"block_days": block, "lower": draws[100], "upper": draws[3899],
            "draws": 4000, "method": "circular_calendar_day_block_bootstrap"}


def booked_lot_pnls(lots: list[dict], captured: datetime) -> dict:
    """Validate every retained resolution, including unassigned partial exits."""
    daily = defaultdict(list)
    for lot in lots:
        resolved = lot.get("closed_at") or lot.get("settled_at")
        if not resolved:
            raise ValueError("resolved lot has no booking timestamp")
        booked = timestamp(resolved)
        if booked > captured:
            raise ValueError("resolved lot is booked after the evidence snapshot")
        daily[booked.date()].append(float(lot["realized_pnl"]))
    return daily


def unassigned_histories(positions: list, known_owners: set, captured: datetime) -> list[dict]:
    """Retain unmatched ownership as attribution; never invent an economic ledger.

    Missing, NULL and empty owner values are distinct raw evidence groups. Even
    when their totals resemble a ledger gap, that is not proof of ownership.
    Dated histories contain only evidenced bookings; no activation date or
    inactive-day calendar denominator is inferred.
    """
    groups = defaultdict(list)
    raw_owners = {}
    for position in positions:
        root = position.root
        owner = root.get("account_id")
        if owner in known_owners:
            continue
        if "account_id" not in root:
            kind = "missing"
        elif owner is None:
            kind = "null"
        elif owner == "":
            kind = "empty"
        else:
            kind = "unmatched"
        key = (kind, json.dumps(owner, sort_keys=True))
        groups[key].append(position)
        raw_owners[key] = owner
    histories = []
    for key, owned in sorted(groups.items()):
        valid = [p for p in owned if p.valid]
        terminal = [p for p in valid if p.terminal]
        lots = [lot for p in valid for lot in p.resolved_lots]
        daily = booked_lot_pnls(lots, captured)
        running = 0.0
        days = []
        for day, pnls in sorted(daily.items()):
            pnl = math.fsum(pnls)
            running += pnl
            days.append({"date": day.isoformat(), "realized_pnl": round(pnl, 6),
                         "resolved_lots": len(pnls),
                         "cumulative_attributed_pnl": round(running, 6)})
        profile_groups = defaultdict(list)
        for p in owned:
            profile_groups[str(p.root.get("risk_profile") or "unknown")].append(p)
        by_profile = {}
        for profile, items in sorted(profile_groups.items()):
            valid_items = [p for p in items if p.valid]
            by_profile[profile] = {
                "valid_roots": len(valid_items), "invalid_roots_excluded": len(items) - len(valid_items),
                "resolved_logical_positions": sum(p.terminal for p in valid_items),
                "net_realized_pnl": round(math.fsum(
                    float(lot["realized_pnl"]) for p in valid_items for lot in p.resolved_lots
                ), 6),
            }
        histories.append({
            "owner_kind": key[0], "raw_owner": raw_owners[key],
            "scope": "Unassigned order attribution only; economic account ownership is unproven.",
            "valid_roots": len(valid), "invalid_roots_excluded": len(owned) - len(valid),
            "resolved_logical_positions": len(terminal), "resolved_lots": len(lots),
            "open_roots_with_resolved_lots": sum(not p.terminal and bool(p.resolved_lots) for p in valid),
            "net_realized_pnl": round(math.fsum(float(lot["realized_pnl"]) for lot in lots), 6),
            "root_statuses": dict(Counter(p.root["status"] for p in valid)),
            "role_profile_breakdown": by_profile,
            "sleeve_root_counts": dict(Counter(str(
                p.root.get("research_sleeve") or p.root.get("sleeve") or "unknown"
            ) for p in valid)),
            "daily_history": days,
        })
    return histories


def build_report(export: dict, *, daily_target: float = 40.0) -> dict:
    if not math.isfinite(daily_target) or daily_target <= 0:
        raise ValueError("daily target must be finite and positive")
    captured = timestamp(export["captured_at"])
    accounts = export["paper"]["paper_accounts"]["rows"]
    positions = group_logical_positions(export["paper"]["paper_orders"]["rows"])
    reports = []
    for account in accounts:
        initial = float(account["initial_capital"])
        if not math.isfinite(initial) or initial <= 0:
            raise ValueError("initial capital must be finite and positive")
        owned = [p for p in positions if p.root.get("account_id") == account["account_id"]]
        valid = [p for p in owned if p.valid]
        terminal = [p for p in valid if p.terminal]
        lots = [lot for p in valid for lot in p.resolved_lots]
        daily = booked_lot_pnls(lots, captured)
        activation = timestamp(account["created_at"])
        if activation > captured:
            raise ValueError("account was activated after the evidence snapshot")
        first_full = activation.date() + timedelta(days=1)
        last_full = captured.date() - timedelta(days=1)
        days = []
        running = float(account["initial_capital"]) + math.fsum(
            math.fsum(v) for d, v in daily.items() if d < first_full
        )
        peak, maximum_drawdown = max(float(account["initial_capital"]), running), 0.0
        cursor = first_full
        while cursor <= last_full:
            pnl = math.fsum(daily.get(cursor, []))
            running += pnl
            peak = max(peak, running)
            maximum_drawdown = max(maximum_drawdown, (peak - running) / peak)
            days.append({"date": cursor.isoformat(), "realized_pnl": round(pnl, 6),
                         "cumulative_realized_pnl": round(running - float(account["initial_capital"]), 6)})
            cursor += timedelta(days=1)
        values = [d["realized_pnl"] for d in days]
        net = math.fsum(float(lot["realized_pnl"]) for lot in lots)
        winning_pnls = [float(p.as_row()["realized_pnl"]) for p in terminal if p.won is True]
        losing_pnls = [float(p.as_row()["realized_pnl"]) for p in terminal if p.won is False]
        average_win = math.fsum(winning_pnls) / len(winning_pnls) if winning_pnls else None
        average_loss = -math.fsum(losing_pnls) / len(losing_pnls) if losing_pnls else None
        capital = math.fsum(float(lot["contracts"]) * float(lot["cost_per_contract"]) for lot in lots)
        entry_fees = math.fsum(float(lot["contracts"]) * float(lot.get("fee_per_contract") or 0) for lot in lots)
        exit_fees = math.fsum(float(lot["contracts"]) * float(lot.get("exit_fee_per_contract") or 0) for lot in lots)
        requested = math.fsum(float(p.root.get("requested_contracts") or 0) for p in valid)
        filled = math.fsum(float(p.root.get("filled_contracts") or 0) for p in valid)
        pnl_by_series = defaultdict(list)
        for p in valid:
            pnl_by_series[str(p.root["market_ticker"]).split("-", 1)[0]].extend(
                float(lot["realized_pnl"]) for lot in p.resolved_lots
            )
        policy_eras = Counter(str(p.root.get("strategy_fingerprint") or "legacy") for p in valid)
        reports.append({
            "account_id": account["account_id"], "status": account["status"],
            "initial_capital": account["initial_capital"], "activated_at": account["created_at"],
            "resolved_logical_positions": len(terminal),
            "winning_logical_positions": sum(p.won is True for p in terminal),
            "losing_logical_positions": sum(p.won is False for p in terminal),
            "gross_winning_pnl": math.fsum(winning_pnls),
            "gross_losing_pnl": math.fsum(losing_pnls),
            "average_winning_pnl": average_win,
            "average_losing_pnl_absolute": average_loss,
            "break_even_hit_rate_at_observed_payouts": average_loss / (average_win + average_loss)
                if average_win is not None and average_loss is not None else None,
            "valid_roots": len(valid), "invalid_roots_excluded": len(owned) - len(valid),
            "root_statuses": dict(Counter(p.root["status"] for p in valid)),
            "net_realized_pnl": round(net, 6), "resolved_entry_capital": round(capital, 6),
            "return_on_initial_capital": net / float(account["initial_capital"]),
            "roi_on_resolved_entry_capital": net / capital if capital else None,
            "entry_fees_on_resolved_lots": round(entry_fees, 6),
            "exit_fees_on_resolved_lots": round(exit_fees, 6),
            "requested_contracts": round(requested, 6), "filled_contracts": round(filled, 6),
            "filled_requested_ratio": filled / requested if requested else None,
            "strategy_fingerprint_root_counts": dict(policy_eras),
            "mean_complete_calendar_day_pnl": math.fsum(values) / len(values) if values else None,
            "complete_calendar_days": len(days),
            "target_dollars_per_day": daily_target,
            "complete_days_meeting_target": sum(pnl >= daily_target for pnl in values),
            "complete_days_meeting_5pct_initial": sum(pnl >= .05 * float(account["initial_capital"]) for pnl in values),
            "pnl_drawdown_on_initial_reference_through_complete_days": maximum_drawdown,
            "mean_daily_pnl_intervals": [mean_interval(values, b) for b in (1, 3, 7) if len(values) >= max(2,b)],
            "turnover_for_target_at_observed_roi": daily_target * capital / net if net > 0 else None,
            "turnover_scenario_note": "Arithmetic scenario only: assumes unchanged realized ROI; neither executable capacity nor future return is established.",
            "series_realized_pnl": {s: round(math.fsum(v), 6) for s,v in sorted(pnl_by_series.items())},
            "activation_day_pnl": round(math.fsum(daily.get(activation.date(), [])), 6),
            "current_partial_day_pnl": round(math.fsum(daily.get(captured.date(), [])), 6),
            "daily_history": days,
        })
    return {
        "schema_version": 1, "captured_at": export["captured_at"],
        "clock": "America/Los_Angeles; resolution-date realized P&L",
        "scope": "Economically separate paper accounts; no combined bankroll or real-money returns.",
        "limitations": [
            "Unrealized marks and unsettled positions are excluded from realized P&L.",
            "Activation day and current partial day are excluded from calendar-day means; zero and outage days stay included.",
            "Mixed strategy fingerprints are reported, not treated as one frozen policy.",
            "Calendar-day blocks are exploratory uncertainty estimates, not prospective strategy qualification.",
            "Win/loss is realized trading P&L, not temperature-probability calibration.",
            "Order-derived cumulative P&L is not ledger equity; published ledger balances, when included, remain authoritative.",
            "Archived-account calendar means include inactive days and cannot be used to compare strategy-era profitability.",
            "Unmatched, NULL, empty and missing owners remain separate attribution-only histories; no bankroll or return denominator is guessed.",
        ], "accounts": reports,
        "unassigned_histories": unassigned_histories(
            positions, {account["account_id"] for account in accounts}, captured
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--daily-target", type=float, default=40.0)
    parser.add_argument("--strategy", type=Path, help="Matching fresh public Strategy artifact for true ledger balances and archived profile histories")
    args = parser.parse_args()
    raw = args.export.read_bytes()
    report = build_report(json.loads(raw), daily_target=args.daily_target)
    report["source_export_sha256"] = hashlib.sha256(raw).hexdigest()
    if args.strategy:
        strategy_raw = args.strategy.read_bytes()
        strategy = json.loads(strategy_raw)
        report["public_strategy_sha256"] = hashlib.sha256(strategy_raw).hexdigest()
        report["public_strategy_generated_at"] = strategy.get("generated_at")
        report["published_ledger_balances"] = list((strategy.get("accounting", {}).get("accounts") or {}).values())
        report["published_profile_results"] = [{
            "risk_profile": p["risk_profile"], "label": p.get("label"),
            "archived": p.get("archived"),
            "summary": (p.get("paper_trading") or {}).get("summary"),
            "daily_history": (p.get("daily_summary") or {}).get("days"),
            "history_window_start": (p.get("daily_summary") or {}).get("window_start"),
            "history_window_end": (p.get("daily_summary") or {}).get("window_end"),
            "learnings": p.get("learnings"), "recommended_changes": p.get("recommended_changes"),
        } for p in strategy.get("profiles", [])]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(f"Wrote {len(report['accounts'])} separate account histories to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
