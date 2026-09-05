"""Audited paper-exit classification shared by reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def audited_exit_reason(row: Any) -> str:
    """Classify one terminal execution lot from persisted lifecycle evidence."""

    status = str(_value(row, "status") or "").upper()
    # A partial expiry cancels only the resting remainder; its filled portion
    # remains an open position until it closes or settles.
    if status == "PAPER_EXPIRED" and not (
        _value(row, "closed_at") or _value(row, "settled_at")
    ):
        return "expired_unfilled"
    if status == "PAPER_SETTLED" or _value(row, "settled_at"):
        return "held_to_settlement"

    if status == "PAPER_CLOSED" or _value(row, "closed_at"):
        # Execution P&L describes the result, not the rule that caused the
        # exit. Inferring the cause makes every losing legacy close a stop,
        # biasing the very reports used to evaluate whether stops help.
        return (
            _explicit_exit_reason(_value(row, "outcome_diagnostics_json"))
            or "unclassified"
        )
    return "unclassified"


def _explicit_exit_reason(raw: object) -> str | None:
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw, Mapping):
        payload = raw
    else:
        return None
    if not isinstance(payload, Mapping):
        return None
    evidence = payload.get("exit_execution")
    candidates = [
        payload.get("exit_reason"),
        payload.get("action"),
        evidence.get("exit_reason") if isinstance(evidence, Mapping) else None,
        evidence.get("monitor_action") if isinstance(evidence, Mapping) else None,
    ]
    aliases = {
        "TAKE_PROFIT": "take_profit",
        "CLOSE_TAKE_PROFIT": "take_profit",
        "STOP_LOSS": "stop_loss",
        "CLOSE_STOP_LOSS": "stop_loss",
        "BREAK_EVEN": "break_even",
        "CLOSE_BREAK_EVEN": "break_even",
    }
    # Match actual actions, not substrings: HOLD_MODEL_VETO means a stop was
    # suppressed, and cannot establish that a later close was a stop-loss.
    reasons = {
        aliases[text]
        for value in candidates
        if (text := "_".join(str(value or "").upper().split())) in aliases
    }
    return next(iter(reasons)) if len(reasons) == 1 else None


def _value(row: Any, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None
