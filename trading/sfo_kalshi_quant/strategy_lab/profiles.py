from __future__ import annotations

from typing import Any

from .._util import _round, _to_float
from . import EXPERIMENTAL_PROFILES, PRIMARY_PROFILE
from .status_alerts import _alert_level, _entry_block_reason, _strategy_alerts


ARCHIVED_PROFILES = frozenset(
    {
        "live-legacy",
        "research",
        "research-target-v1",
        "research-target-v2",
        "research-target-v3",
        "research-target-v4",
        "research-target-v5",
        "research-motion",
    }
)


def _profile_views(
    *,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> list[dict[str, Any]]:
    names = _profile_names(daily_summary, paper, signal_quality)
    return [
        _profile_view(
            name,
            daily_summary=daily_summary,
            paper=paper,
            signal_quality=signal_quality,
        )
        for name in names
    ]


def _profile_names(
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> list[str]:
    names: set[str] = set()

    def add(value: object) -> None:
        name = _profile_key(value)
        names.add(name)

    for row in daily_summary.get("profiles") or []:
        add(row.get("risk_profile"))
    for row in paper.get("profiles") or []:
        add(row.get("risk_profile"))
    for bucket in ("open_positions", "closed_positions", "recent_monitor_actions"):
        for row in paper.get(bucket) or []:
            add(row.get("risk_profile"))
    for row in paper.get("pending_limit_orders") or []:
        add(row.get("risk_profile"))
    for name in (signal_quality.get("latest_candidates_by_profile") or {}):
        add(name)
    # An archived profile resolves nothing inside the shared window, so it can
    # be absent from every window-scoped list above. Its era keeps it visible.
    for name in (daily_summary.get("profile_eras") or {}):
        add(name)
    for row in signal_quality.get("latest_candidates") or []:
        add(row.get("risk_profile"))
    names.discard("unknown")
    return sorted(names, key=_profile_sort_key)


def _profile_view(
    name: str,
    *,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> dict[str, Any]:
    archived = name in ARCHIVED_PROFILES
    # An archived profile no longer trades, so the shared trailing window
    # resolves nothing for it and its curve would flatten to a single value.
    # Its own era series carries the shape it actually earned.
    era = (daily_summary.get("profile_eras") or {}).get(name) if archived else None
    profile_daily = _profile_daily_summary(daily_summary, paper, name, era=era)
    profile_paper = _profile_paper_payload(paper, name)
    profile_signal = _profile_signal_quality(signal_quality, name)
    learnings = _profile_learnings(
        name,
        daily_summary=profile_daily,
        paper=profile_paper,
        signal_quality=profile_signal,
    )
    recommendations = _profile_recommendations(name, profile_daily)
    profile_daily["learnings"] = learnings
    profile_daily["recommended_changes"] = recommendations
    return {
        "risk_profile": name,
        "label": _profile_label(name),
        "profile_type": "experimental" if _is_experimental(name) else "primary",
        "archived": archived,
        "daily_summary": profile_daily,
        "signal_quality": profile_signal,
        "paper_trading": profile_paper,
        "learnings": learnings,
        "recommended_changes": recommendations,
        "status": _profile_status(name, profile_daily, profile_paper, profile_signal),
        "daily_target": (
            paper.get("research_daily_target")
            if name == "research-target"
            else None
        ),
        "excluded_from": (
            ["active_admissions", "daily_target", "live_readiness"]
            if archived
            else ["live_readiness"]
            if name == "research-target"
            else []
        ),
    }


def _profile_daily_summary(
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    name: str,
    era: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile_total = _profile_row(daily_summary.get("profiles") or [], name)
    paper_total = _profile_row(paper.get("profiles") or [], name)
    window_pnl = _to_float(profile_total.get("realized_pnl"))
    all_time_pnl = _to_float(paper_total.get("realized_pnl", window_pnl))
    if era and (era.get("days") or []):
        return _era_daily_summary(
            daily_summary,
            era,
            name,
            all_time_pnl=all_time_pnl,
            # Only an actually-published paper total may override the era's own
            # arithmetic. Without this guard a paper payload missing the
            # profile would pin the era's terminal point to a fabricated zero.
            reconcile_to_paper_total="realized_pnl" in paper_total,
        )
    cumulative = all_time_pnl - window_pnl
    days = []
    for row in daily_summary.get("days") or []:
        profile = ((row.get("profiles") or {}).get(name) or {})
        realized = _to_float(profile.get("realized_pnl"))
        opening_attribution = cumulative
        cumulative += realized
        days.append(
            {
                "date": row.get("date"),
                "opened": int(_to_float(profile.get("opened"))),
                "closed": int(_to_float(profile.get("closed"))),
                "settled": int(_to_float(profile.get("settled"))),
                "resolved": int(_to_float(profile.get("resolved"))),
                "wins": int(_to_float(profile.get("wins"))),
                "losses": int(_to_float(profile.get("losses"))),
                "hit_rate": profile.get("hit_rate"),
                "realized_pnl": _round(realized, 2),
                "opening_attributed_pnl": _round(opening_attribution, 2),
                "cumulative_realized": _round(cumulative, 2),
                "closing_attributed_pnl": _round(cumulative, 2),
                "opened_spend": _round(profile.get("opened_spend"), 2),
                "resolved_spend": _round(profile.get("resolved_spend"), 2),
                "roi": profile.get("roi"),
                "signals": int(_to_float(profile.get("signals"))),
                "approved_signals": int(_to_float(profile.get("approved_signals"))),
                "forecast_predicted_high_f": row.get("forecast_predicted_high_f"),
                "forecast_actual_high_f": row.get("forecast_actual_high_f"),
                "forecast_error_f": row.get("forecast_error_f"),
            }
        )
    if days:
        # Daily rows are already publication-rounded upstream, so their sum can
        # drift a cent from the canonical order-level profile total. The curve's
        # terminal point must reconcile to the same attribution total used by
        # the summary and account-scoped balance surfaces.
        terminal_attribution = _round(all_time_pnl, 2)
        days[-1]["cumulative_realized"] = terminal_attribution
        days[-1]["closing_attributed_pnl"] = terminal_attribution
    resolved = int(_to_float(profile_total.get("resolved")))
    wins = int(_to_float(profile_total.get("wins")))
    losses = int(_to_float(profile_total.get("losses")))
    opened = sum(int(row["opened"]) for row in days)
    closed = sum(int(row["closed"]) for row in days)
    settled = sum(int(row["settled"]) for row in days)
    resolved_capital = _to_float(profile_total.get("capital_resolved"))
    totals = {
        "trades_opened": opened,
        "trades_closed": closed,
        "trades_settled": settled,
        "open_positions": int(_to_float(paper_total.get("open_positions"))),
        "open_risk": _round(paper_total.get("open_risk"), 2),
        "realized_pnl": _round(window_pnl, 2),
        "cumulative_realized_pnl": _round(all_time_pnl, 2),
        "all_time_attributed_pnl": _round(all_time_pnl, 2),
        "window_attributed_pnl": _round(window_pnl, 2),
        "capital_resolved": _round(resolved_capital, 2),
        "roi": _round(window_pnl / resolved_capital, 4) if resolved_capital > 0 else None,
        "wins": wins,
        "losses": losses,
        "hit_rate": _round(wins / (wins + losses), 4) if (wins + losses) else None,
        "mean_abs_forecast_error_f": (daily_summary.get("totals") or {}).get(
            "mean_abs_forecast_error_f"
        ),
    }
    return {
        "available": bool(daily_summary.get("available", True)),
        "schema_version": daily_summary.get("schema_version"),
        "generated_at": daily_summary.get("generated_at"),
        "window_days": daily_summary.get("window_days"),
        "window_start": daily_summary.get("window_start"),
        "window_end": daily_summary.get("window_end"),
        "bankroll": daily_summary.get("bankroll"),
        # Legacy profiles contribute P&L to the shared/legacy account. Explicit
        # research sleeves have separately published $1,000 account state.
        "opening_attributed_pnl": _round(all_time_pnl - window_pnl, 2),
        "current_attributed_pnl": _round(all_time_pnl, 2),
        # Profile-scoped YES/NO split and exit-reason mix so these cards render on
        # a profile tab, not just the All-profiles overview (the template reads
        # these field names directly).
        "side_performance": (daily_summary.get("side_performance_by_profile") or {}).get(name)
        or {},
        "exit_reasons": (daily_summary.get("exit_reasons_by_profile") or {}).get(name) or {},
        "risk_profile": name,
        "days": days,
        "totals": totals,
        "profiles": [profile_total] if profile_total else [],
        "gate_behavior": _profile_gate_behavior(daily_summary, name),
        "model_vs_market": daily_summary.get("model_vs_market") or {},
        "data_collected": daily_summary.get("data_collected") or {},
        "biggest_winners": [
            row
            for row in daily_summary.get("biggest_winners") or []
            if _profile_key(row.get("risk_profile")) == name
        ],
        "biggest_losers": [
            row
            for row in daily_summary.get("biggest_losers") or []
            if _profile_key(row.get("risk_profile")) == name
        ],
    }


def _era_daily_summary(
    daily_summary: dict[str, Any],
    era: dict[str, Any],
    name: str,
    *,
    all_time_pnl: float,
    reconcile_to_paper_total: bool,
) -> dict[str, Any]:
    """Daily summary for an archived profile, scoped to its own active era.

    The window here is the profile's real lifetime rather than the shared
    trailing window, so `window_start`/`window_end` state when the experiment
    actually ran and the curve keeps its shape. Because the era spans the whole
    life of the profile, its window P&L and its all-time P&L are the same
    number by construction.
    """

    days = [dict(row) for row in era.get("days") or []]
    era_totals = era.get("totals") or {}
    # Daily rows are publication-rounded, so their sum can drift a cent from
    # the canonical order-level total. Where the paper payload publishes that
    # total it wins, so the curve's terminal point, the window figure, and the
    # account-scoped balance surfaces all state one number.
    era_pnl = (
        _round(all_time_pnl, 2)
        if reconcile_to_paper_total
        else _round(_to_float(era_totals.get("realized_pnl")), 2)
    )
    if days:
        days[-1]["cumulative_realized"] = era_pnl
        days[-1]["closing_attributed_pnl"] = era_pnl
    if not reconcile_to_paper_total:
        all_time_pnl = era_pnl
    resolved_capital = _to_float(era_totals.get("capital_resolved"))
    wins = int(_to_float(era_totals.get("wins")))
    losses = int(_to_float(era_totals.get("losses")))
    totals = {
        "trades_opened": int(_to_float(era_totals.get("trades_opened"))),
        "trades_closed": int(_to_float(era_totals.get("trades_closed"))),
        "trades_settled": int(_to_float(era_totals.get("trades_settled"))),
        "open_positions": 0,
        "open_risk": 0.0,
        "realized_pnl": _round(era_pnl, 2),
        "cumulative_realized_pnl": _round(all_time_pnl, 2),
        "all_time_attributed_pnl": _round(all_time_pnl, 2),
        "window_attributed_pnl": _round(era_pnl, 2),
        "capital_resolved": _round(resolved_capital, 2),
        "roi": era_totals.get("roi"),
        "wins": wins,
        "losses": losses,
        "hit_rate": era_totals.get("hit_rate"),
        "mean_abs_forecast_error_f": None,
    }
    return {
        "available": bool(daily_summary.get("available", True)),
        "schema_version": daily_summary.get("schema_version"),
        "generated_at": daily_summary.get("generated_at"),
        "window_basis": "profile_era",
        "window_days": era.get("window_days"),
        "window_start": era.get("window_start"),
        "window_end": era.get("window_end"),
        "bankroll": daily_summary.get("bankroll"),
        # The era opens at the profile's first resolution, so it starts flat at
        # zero attribution and closes on its all-time total.
        "opening_attributed_pnl": 0.0,
        "current_attributed_pnl": _round(all_time_pnl, 2),
        "side_performance": (daily_summary.get("side_performance_by_profile") or {}).get(name)
        or {},
        "exit_reasons": (daily_summary.get("exit_reasons_by_profile") or {}).get(name) or {},
        "risk_profile": name,
        "days": days,
        "totals": totals,
        "profiles": [],
        "gate_behavior": _profile_gate_behavior(daily_summary, name),
        "model_vs_market": daily_summary.get("model_vs_market") or {},
        "data_collected": daily_summary.get("data_collected") or {},
        "biggest_winners": [],
        "biggest_losers": [],
    }


def _profile_paper_payload(paper: dict[str, Any], name: str) -> dict[str, Any]:
    open_rows = [
        row
        for row in paper.get("open_positions") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    pending_limit_rows = [
        row
        for row in paper.get("pending_limit_orders") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    closed_rows = [
        row
        for row in paper.get("closed_positions") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    action_rows = [
        row
        for row in paper.get("recent_monitor_actions") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    monitor_action_rows = [
        row
        for row in action_rows
        if row.get("status") not in {"OPEN", "LIMIT_RESTING"}
    ]
    profile = _profile_row(paper.get("profiles") or [], name)
    duplicate_rows = [
        row
        for row in paper.get("duplicate_open_groups") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    open_positions = int(_to_float(profile.get("open_positions")))
    pending_limit_count = int(_to_float(profile.get("pending_limit_orders")))
    marked_open = [row for row in open_rows if row.get("unrealized_pnl") is not None]
    unrealized_pnl = (
        _round(sum(_to_float(row.get("unrealized_pnl")) for row in marked_open), 2)
        if marked_open
        else None
    )
    open_value = (
        _round(sum(_to_float(row.get("current_value")) for row in marked_open), 2)
        if marked_open
        else None
    )
    summary = {
        "open_positions": open_positions,
        "published_open_positions": len(open_rows),
        "hidden_open_positions": max(0, open_positions - len(open_rows)),
        "pending_limit_orders": pending_limit_count,
        "published_pending_limit_orders": len(pending_limit_rows),
        "hidden_pending_limit_orders": max(0, pending_limit_count - len(pending_limit_rows)),
        "pending_limit_risk": _round(profile.get("pending_limit_risk"), 2),
        "duplicate_open_groups": len(duplicate_rows),
        "largest_duplicate_open_group": max(
            [row["open_orders"] for row in duplicate_rows],
            default=0,
        ),
        "unresolved_past_targets": [],
        "latest_opened_at": open_rows[0].get("created_at") if open_rows else None,
        "latest_monitor_action_at": (
            monitor_action_rows[0].get("time") if monitor_action_rows else None
        ),
        "closed_positions": int(_to_float(profile.get("orders"))),
        "realized_pnl": _round(profile.get("realized_pnl"), 2),
        "unrealized_pnl": unrealized_pnl,
        "marked_open_positions": len(marked_open),
        "open_risk": _round(profile.get("open_risk"), 2),
        "open_value": open_value,
        "capital_at_risk": _round(profile.get("capital_resolved"), 2),
        "roi": profile.get("roi"),
        "hit_rate": profile.get("hit_rate"),
        "win_count": int(_to_float(profile.get("wins"))),
        "loss_count": int(_to_float(profile.get("losses"))),
    }
    return {
        "available": bool(paper.get("available")),
        "monitor": paper.get("monitor") or {},
        "summary": summary,
        "open_positions": open_rows,
        "pending_limit_orders": pending_limit_rows,
        "closed_positions": closed_rows,
        "recent_monitor_actions": action_rows,
        "duplicate_open_groups": duplicate_rows,
        "profiles": [profile] if profile else [],
        "daily_target": profile.get("daily_target"),
        "excluded_from": profile.get("excluded_from") or [],
    }


def _profile_signal_quality(signal_quality: dict[str, Any], name: str) -> dict[str, Any]:
    by_profile = signal_quality.get("latest_candidates_by_profile") or {}
    rows = by_profile.get(name)
    if rows is None:
        rows = [
            row
            for row in signal_quality.get("latest_candidates") or []
            if _profile_key(row.get("risk_profile")) == name
        ]
    return {
        "available": bool(rows),
        "source": signal_quality.get("source"),
        "latest_candidates": rows,
        # The Kalshi market consensus is the same ladder for every paper profile
        # (it is the crowd's view of the settlement high, not a per-book metric),
        # so pass the parent block straight through to each profile view.
        "market_consensus": signal_quality.get("market_consensus") or {"available": False},
        "charts": {
            "probability_vs_market": _probability_market_points(rows),
            "edge_by_market_bucket": _edge_by_market_bucket(rows),
            "quality_distribution": _quality_distribution(rows),
        },
    }


def _profile_gate_behavior(daily_summary: dict[str, Any], name: str) -> dict[str, Any]:
    gate = daily_summary.get("gate_behavior") or {}
    row = _profile_row(gate.get("by_profile") or [], name)
    return {
        "approved": int(_to_float(row.get("approved"))),
        "rejected": max(0, int(_to_float(row.get("signals"))) - int(_to_float(row.get("approved")))),
        "top_rejections": row.get("top_rejections") or [],
        "top_rejections_all": row.get("top_rejections_all") or [],
        "rejection_categories": row.get("rejection_categories") or {},
        "entry_block_reasons": row.get("entry_block_reasons") or [],
        "by_profile": [row] if row else [],
    }


def _profile_learnings(
    name: str,
    *,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> list[str]:
    totals = daily_summary.get("totals") or {}
    resolved = int(_to_float(totals.get("wins"))) + int(_to_float(totals.get("losses")))
    notes: list[str] = []
    if resolved:
        notes.append(
            f"{name} resolved {resolved} trade(s): "
            f"{int(_to_float(totals.get('wins')))}W / "
            f"{int(_to_float(totals.get('losses')))}L, "
            f"net ${_to_float(totals.get('realized_pnl')):+.2f}."
        )
    else:
        notes.append(f"{name} has no resolved trades in this window yet.")
    open_risk = _to_float((paper.get("summary") or {}).get("open_risk"))
    if open_risk:
        notes.append(f"{name} currently has ${open_risk:.2f} of paper open risk.")
    signal_count = len(signal_quality.get("latest_candidates") or [])
    if signal_count:
        notes.append(f"{name} has {signal_count} current signal candidate(s) in the latest artifact.")
    if _is_experimental(name):
        notes.append(
            f"{name} is experimental paper-data collection; its P&L is isolated from the balanced headline."
        )
    return notes


def _profile_recommendations(name: str, daily_summary: dict[str, Any]) -> list[str]:
    totals = daily_summary.get("totals") or {}
    resolved = int(_to_float(totals.get("wins"))) + int(_to_float(totals.get("losses")))
    roi = totals.get("roi")
    if resolved == 0:
        return [f"Keep collecting {name} scans before changing this profile's gates."]
    if resolved < 15:
        return [
            f"{name} has only {resolved} resolved trade(s); wait for at least 15 before changing gates."
        ]
    if roi is not None and _to_float(roi) < -0.05:
        return [f"{name} ROI is materially negative; inspect losers before loosening this profile."]
    return [f"No rule-based {name} gate change is indicated by this window."]


def _profile_status(
    name: str,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> dict[str, Any]:
    totals = daily_summary.get("totals") or {}
    paper_summary = paper.get("summary") or {}
    entry_block_reason = _entry_block_reason(signal_quality.get("latest_candidates") or [])
    alerts = _strategy_alerts(
        paper=paper,
        entry_block_reason=entry_block_reason,
    )
    open_count = int(_to_float(paper_summary.get("open_positions")))
    pending_count = int(_to_float(paper_summary.get("pending_limit_orders")))
    if open_count and pending_count:
        paper_status = (
            f"{open_count} open {name} paper position(s); "
            f"{pending_count} resting limit order(s)"
        )
    elif open_count:
        paper_status = f"{open_count} open {name} paper position(s)"
    elif pending_count:
        paper_status = f"{pending_count} resting limit order(s) for {name}"
    else:
        paper_status = f"no open {name} paper positions"
    return {
        "risk_profile": name,
        "profile_label": _profile_label(name),
        "profile_type": "experimental" if _is_experimental(name) else "primary",
        "paper_trading_status": paper_status,
        "open_risk": _round(paper_summary.get("open_risk"), 2),
        "pending_limit_risk": _round(paper_summary.get("pending_limit_risk"), 2),
        "realized_pnl": _round(totals.get("realized_pnl"), 2),
        "hit_rate": totals.get("hit_rate"),
        "latest_signal_count": len(signal_quality.get("latest_candidates") or []),
        "entry_scanner_reason": entry_block_reason,
        "alerts": alerts,
        "alert_level": _alert_level(alerts),
    }


def _profile_row(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for row in rows:
        if _profile_key(row.get("risk_profile")) == name:
            return dict(row)
    return {
        "risk_profile": name,
        "orders": 0,
        "resolved": 0,
        "wins": 0,
        "losses": 0,
        "hit_rate": None,
        "realized_pnl": 0.0,
        "capital_resolved": 0.0,
        "roi": None,
        "open_positions": 0,
        "open_risk": 0.0,
        "pending_limit_orders": 0,
        "pending_limit_risk": 0.0,
        "signals": 0,
        "approved": 0,
    }


def _default_profile(profiles: list[dict[str, Any]]) -> str:
    names = {row["risk_profile"] for row in profiles}
    if PRIMARY_PROFILE in names:
        return PRIMARY_PROFILE
    return profiles[0]["risk_profile"] if profiles else PRIMARY_PROFILE


def _profile_key(value: object) -> str:
    return str(value or "unknown").strip().lower() or "unknown"


def _is_experimental(name: str) -> bool:
    return name in EXPERIMENTAL_PROFILES or name.startswith("research-")


def _profile_sort_key(name: str) -> tuple[int, str]:
    order = {
        "live": 0,
        "research-target": 1,
        "live-legacy": 2,
        "research-target-v1": 3,
        "research-target-v2": 4,
        "research-target-v3": 5,
        "research-target-v4": 6,
        "research-target-v5": 7,
        "research-motion": 8,
        "research": 9,
        "unknown": 9,
    }
    return order.get(name, 8), name


def _profile_label(name: str) -> str:
    if name == "live":
        return "Live Stability"
    if name == "live-legacy":
        return "Prior readiness benchmark"
    if name == "research":
        return "Legacy research (archived)"
    if name == "research-target":
        return "Research ROI · fixed daily objective"
    if name == "research-target-v1":
        return "Research target v1 (archived control)"
    if name == "research-target-v2":
        return "Research target v2 (archived)"
    if name == "research-target-v3":
        return "Research ROI v3 (archived oversize experiment)"
    if name == "research-target-v4":
        return "Research ROI v4 (archived breadth restoration)"
    if name == "research-target-v5":
        return "Research ROI v5 (archived 1.5x step)"
    if name == "research-motion":
        return "Research motion (archived execution learning)"
    return name


def _probability_market_points(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for row in decisions:
        market = row.get("market_probability")
        model = row.get("model_probability")
        probability = row.get("probability")
        if market is None or (model is None and probability is None):
            continue
        points.append(
            {
                "x": _round(market, 4),
                "y": _round(model if model is not None else probability, 4),
                "r": max(4, min(12, _to_float(row.get("quality_score")) / 10)),
                "label": row.get("label"),
                "side": row.get("side"),
                "approved": bool(row.get("approved")),
            }
        )
    return points


def _edge_by_market_bucket(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
    output = []
    for lower, upper in buckets:
        rows = [
            row
            for row in decisions
            if row.get("market_probability") is not None
            and lower / 100 <= _to_float(row["market_probability"]) < upper / 100
        ]
        output.append(
            {
                "range": f"{lower}-{upper}",
                "count": len(rows),
                "avg_edge": _round(
                    sum(_to_float(row.get("edge")) for row in rows) / len(rows),
                    4,
                )
                if rows
                else 0.0,
            }
        )
    return output


def _quality_distribution(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100.0001)]
    output = []
    for lower, upper in buckets:
        rows = [
            row
            for row in decisions
            if lower <= _to_float(row.get("quality_score")) < upper
        ]
        output.append({"range": f"{int(lower)}-{int(min(upper, 100))}", "count": len(rows)})
    return output
