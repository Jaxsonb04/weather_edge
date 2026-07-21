from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .._util import (
    _date_from_string,
    _parse_timestamp,
    _round,
    _to_float,
)
from ..config import StrategyConfig
from ..settlement_day import settlement_today
from . import ACTIVE_CALIBRATION_SOURCE, CHALLENGER_CALIBRATION_SOURCE


def _status_payload(
    *,
    config: StrategyConfig,
    db_path: Path,
    trading_signal: dict[str, Any] | None,
    backtest: dict[str, Any],
    signal_quality: dict[str, Any],
    paper: dict[str, Any],
    forecast_health: dict[str, Any],
) -> dict[str, Any]:
    latest_targets = [
        str(row.get("target_date"))
        for row in signal_quality.get("latest_candidates", [])
        if row.get("target_date") and row.get("market_available") is not False
    ]
    entry_block_reason = _entry_block_reason(signal_quality.get("latest_candidates", []))
    for row in paper.get("open_positions", []):
        if row.get("target_date") and not _is_probability_only_ticker(row.get("ticker")):
            latest_targets.append(str(row["target_date"]))
    latest_target = signal_quality.get("latest_target_date") or _status_target_date(
        latest_targets,
        entry_block_reason=entry_block_reason,
    ) or _target_from_signal(trading_signal)
    raw_count = backtest["counts"]["raw_signals"]
    settled_count = backtest["counts"]["settled_signals"]
    small_sample = settled_count < 30
    alerts = _strategy_alerts(
        paper=paper,
        entry_block_reason=entry_block_reason,
        forecast_health=forecast_health,
    )
    return {
        "active_calibration_source": ACTIVE_CALIBRATION_SOURCE,
        "active_calibration_label": "lstm = Active execution calibration",
        "challenger_calibration_source": CHALLENGER_CALIBRATION_SOURCE,
        "challenger_calibration_label": (
            "clean-blend/combined = Challenger research calibration"
        ),
        "aws_execution_calibration_locked": True,
        "paper_only": True,
        "automation_status": (
            "AWS timers generate forecast, public signal, Strategy Lab JSON, "
            "paper scans, and paper monitor state when enabled."
        ),
        "paper_trading_status": _paper_status(paper),
        "entry_scanner_status": (
            "Same-day entries blocked; rolling scanner is evaluating later target dates."
            if entry_block_reason
            else "Entry scanner active for eligible target dates."
        ),
        "entry_scanner_reason": entry_block_reason,
        "last_updated": datetime.now(UTC).isoformat(),
        "latest_target_date": latest_target,
        "latest_signal_targets": sorted(set(latest_targets)),
        "raw_signal_count": raw_count,
        "pre_resolution_signal_count": backtest["counts"]["pre_resolution_signals"],
        "deduped_signal_count": backtest["counts"]["deduped_signals"],
        "post_resolution_excluded_count": backtest["counts"]["excluded_post_resolution_signals"],
        "alerts": alerts,
        "alert_level": _alert_level(alerts),
        "sample_warning": (
            "Sample size is still small; treat calibration and ROI as research diagnostics."
            if small_sample
            else ""
        ),
        "bankroll": _round(config.paper_bankroll, 2),
        "target_exposure_cap": _round(config.paper_bankroll * config.max_target_exposure_pct, 2),
        "max_entries_per_market_side": int(config.max_entries_per_market_side),
        "open_risk": paper["summary"]["open_risk"],
        "db_path_hint": str(db_path),
    }


def _decision_reason(approved: bool, reasons: list[str], edge: object, edge_lcb: object) -> str:
    if approved:
        return (
            f"Passed risk gates with edge {_to_float(edge):.3f} and "
            f"lower-bound edge {_to_float(edge_lcb):.3f}."
        )
    if reasons:
        return reasons[0]
    return "No trade gate reason was recorded."


def _why_trade_good(row: sqlite3.Row, reasons: list[str]) -> str:
    if reasons:
        return "; ".join(reasons[:2])
    return (
        f"Paper position passed gates with p={_to_float(row['probability']):.3f}, "
        f"edge={_to_float(row['edge']):.3f}, "
        f"edge_lcb={_to_float(row['edge_lcb']):.3f}."
    )


def _paper_status(paper: dict[str, Any]) -> str:
    if not paper.get("available"):
        return "paper database unavailable"
    summary = paper["summary"]
    open_count = int(_to_float(summary.get("open_positions"), default=0.0))
    pending_count = int(_to_float(summary.get("pending_limit_orders"), default=0.0))
    if open_count and pending_count:
        return f"{open_count} open paper position(s); {pending_count} resting limit order(s)"
    if open_count:
        return f"{open_count} open paper position(s)"
    if pending_count:
        return f"{pending_count} resting limit order(s)"
    return "no open paper positions"


def _strategy_alerts(
    *,
    paper: dict[str, Any],
    entry_block_reason: str | None,
    daily_budget: float | None = None,
    now: datetime | None = None,
    forecast_health: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    current_utc = now or datetime.now(UTC)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=UTC)
    else:
        current_utc = current_utc.astimezone(UTC)
    summary = paper.get("summary") or {}
    if not paper.get("available"):
        alerts.append(
            _alert(
                "warning",
                "paper-db-unavailable",
                "Paper DB unavailable",
                str(paper.get("reason") or "Strategy Lab cannot read paper-trading state."),
                "Check the AWS paper DB path and strategy-research service logs.",
            )
        )
        return alerts

    open_count = int(_to_float(summary.get("open_positions"), default=0.0))
    unresolved_targets = summary.get("unresolved_past_targets") or []
    if unresolved_targets:
        target_text = ", ".join(
            f"{row.get('target_date')} ({int(row.get('open_orders') or 0)})"
            for row in unresolved_targets[:4]
        )
        # A paper position settles the MORNING AFTER its target date, once the NWS
        # CLISFO daily climate report for that date is published (it cannot exist
        # earlier -- the day's high is not known until the day ends). So a position
        # whose target was yesterday is in NORMAL settlement lag, not a failure: the
        # paper-settle timer clears it within hours. Flagging that benign, expected
        # state as a CRITICAL "backlog" is a false alarm. Escalate to critical only
        # when a target is >= 2 days stale, i.e. the settlement-high lookup genuinely
        # failed to resolve it. See docs/trading_engine_diagnosis_2026-06-16.md.
        # Use the injected clock (current_utc) so the age threshold is testable and
        # consistent with _entry_block_reason rather than reading the wall clock.
        today = settlement_today(current_utc)
        ages = []
        for row in unresolved_targets:
            parsed = _date_from_string(row.get("target_date"))
            if parsed is not None:
                ages.append((today - parsed).days)
        max_age = max(ages, default=1)
        if max_age >= 2:
            alerts.append(
                _alert(
                    "critical",
                    "settlement-backlog",
                    "Settlement backlog",
                    f"Paper positions are up to {max_age} days past settlement for completed "
                    f"target dates: {target_text}. The settlement-high lookup could not resolve "
                    f"them from CLISFO or WeatherEdge ground truth.",
                    "Run paper-auto-settle (it backfills older CLISFO versions) or inspect the "
                    "settlement source for those dates.",
                )
            )
        else:
            alerts.append(
                _alert(
                    "warning",
                    "settlement-pending",
                    "Settlement pending",
                    f"Positions for {target_text} are awaiting the official CLISFO high, which "
                    f"publishes the morning after the target date. Auto-settle resolves them on "
                    f"its next run.",
                    "No action needed; the paper-settle timer settles these automatically.",
                )
            )

    duplicate_groups = paper.get("duplicate_open_groups") or []
    if duplicate_groups:
        largest = duplicate_groups[0]
        alerts.append(
            _alert(
                "critical",
                "duplicate-open-markets",
                "Duplicate open markets",
                (
                    f"{len(duplicate_groups)} market/side group(s) have repeated open positions. "
                    f"Largest: {largest.get('open_orders')}x {largest.get('ticker')} {largest.get('side')}."
                ),
                "Clear legacy duplicates, then confirm the duplicate guard is deployed.",
            )
        )

    latest_monitor_at = summary.get("latest_monitor_action_at")
    latest_monitor_dt = _parse_timestamp(latest_monitor_at)
    if open_count and latest_monitor_dt is None:
        latest_opened_dt = _parse_timestamp(summary.get("latest_opened_at"))
        if latest_opened_dt is not None and current_utc - latest_opened_dt <= timedelta(minutes=10):
            alerts.append(
                _alert(
                    "info",
                    "monitor-pending",
                    "Monitor mark pending",
                    "A paper position was opened recently; the next monitor pass should mark it shortly.",
                    "Keep the paper monitor timer active and refresh Strategy Lab after the next monitor tick.",
                )
            )
        else:
            alerts.append(
                _alert(
                    "critical",
                    "monitor-not-recording",
                    "Monitor not recording",
                    "Open paper positions exist, but Strategy Lab has no monitor inspection rows.",
                    "Start the paper monitor service and refresh Strategy Lab.",
                )
            )
    elif open_count and latest_monitor_dt is not None:
        monitor_age = current_utc - latest_monitor_dt
        if monitor_age > timedelta(minutes=45):
            alerts.append(
                _alert(
                    "critical",
                    "monitor-stale",
                    "Monitor stale",
                    f"Latest paper monitor action is {_age_label(monitor_age)} old.",
                    "Check the paper monitor timer and service logs.",
                )
            )

    marked_count = int(_to_float(summary.get("marked_open_positions"), default=0.0))
    if open_count and marked_count == 0:
        alerts.append(
            _alert(
                "warning",
                "open-positions-unmarked",
                "Open positions unmarked",
                "Open paper positions have no current sell-bid marks yet.",
                "Confirm monitor snapshots include live bid data.",
            )
        )

    hidden_count = int(_to_float(summary.get("hidden_open_positions"), default=0.0))
    if hidden_count:
        alerts.append(
            _alert(
                "warning",
                "open-positions-hidden",
                "Open list truncated",
                f"{hidden_count} open paper position(s) are summarized but hidden from the card list.",
                "Use paper-report for the full ledger, or reduce stale open inventory.",
            )
        )

    open_risk = _to_float(summary.get("open_risk"), default=0.0)
    if daily_budget is not None and daily_budget > 0 and open_risk > daily_budget:
        alerts.append(
            _alert(
                "warning",
                "open-risk-over-budget",
                "Open risk over budget",
                f"Open paper risk ${open_risk:.2f} is above the daily budget ${daily_budget:.2f}.",
                "Review duplicate exposure and daily budget settings.",
            )
        )

    if entry_block_reason:
        alerts.append(
            _alert(
                "info",
                "same-day-entry-blocked",
                "Same-day entries blocked",
                entry_block_reason,
                "Monitor and settlement can still run; scanner shifts to later targets.",
            )
        )

    alerts.extend(_forecast_health_alerts(forecast_health))

    if not alerts:
        alerts.append(
            _alert(
                "ok",
                "strategy-lab-healthy",
                "Strategy Lab healthy",
                "No settlement, monitor, duplicate-position, or risk alerts are active.",
                "Keep monitoring after each AWS refresh.",
            )
        )
    return alerts


def _forecast_health_alerts(forecast_health: dict[str, Any] | None) -> list[dict[str, str]]:
    if not forecast_health:
        return []
    output: list[dict[str, str]] = []
    for warning in forecast_health.get("warnings") or []:
        if not isinstance(warning, dict):
            continue
        output.append(
            _alert(
                str(warning.get("level") or "warning"),
                str(warning.get("code") or "forecast-health-warning"),
                str(warning.get("title") or "Forecast health warning"),
                str(warning.get("detail") or "Forecast health check reported a warning."),
                str(warning.get("action") or "Inspect AWS forecast refresh logs."),
            )
        )
    return output


def _alert(level: str, code: str, title: str, detail: str, action: str) -> dict[str, str]:
    return {
        "level": level,
        "code": code,
        "title": title,
        "detail": detail,
        "action": action,
    }


def _alert_level(alerts: list[dict[str, str]]) -> str:
    order = {"critical": 4, "warning": 3, "info": 2, "ok": 1}
    return max((alert.get("level", "ok") for alert in alerts), key=lambda level: order.get(level, 0), default="ok")


def _entry_block_reason(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> str | None:
    today = settlement_today(now)
    for row in rows:
        target = _date_from_string(row.get("target_date"))
        if target is not None and target != today:
            continue
        explicit = row.get("entry_block_reason")
        if explicit:
            return str(explicit)
        for reason in row.get("reasons") or []:
            text = str(reason)
            if text.startswith("same-day entry disabled:"):
                return text
    return None


def _status_target_date(
    targets: list[str],
    *,
    entry_block_reason: str | None,
    now: datetime | None = None,
) -> str | None:
    parsed = sorted({
        parsed
        for target in targets
        if (parsed := _date_from_string(target)) is not None
    })
    if not parsed:
        return None

    today = settlement_today(now)

    if entry_block_reason:
        future = [target for target in parsed if target > today]
        if future:
            return future[0].isoformat()
    elif today in parsed:
        return today.isoformat()

    current_or_future = [target for target in parsed if target >= today]
    if current_or_future:
        return current_or_future[0].isoformat()
    return parsed[-1].isoformat()


def _target_from_signal(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    targets = [
        target.get("target_date")
        for target in payload.get("targets") or []
        if target.get("target_date") and target.get("market_available") is not False
    ]
    return max(targets) if targets else None


def _is_probability_only_ticker(ticker: object) -> bool:
    return "-PAPER" in str(ticker or "")


def _age_label(delta: timedelta) -> str:
    total_minutes = max(0, int(delta.total_seconds() // 60))
    if total_minutes < 60:
        return f"{total_minutes} minute(s)"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours} hour(s) {minutes} minute(s)"
    days, hours = divmod(hours, 24)
    return f"{days} day(s) {hours} hour(s)"
