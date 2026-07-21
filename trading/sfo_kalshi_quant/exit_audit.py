"""Audited paper-exit classification shared by reports."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any


def audited_exit_reason(row: Any) -> str:
    """Classify one terminal execution lot from persisted lifecycle evidence."""

    status = str(_value(row, "status") or "").upper()
    if status in {"PAPER_EXPIRED", "PAPER_PARTIAL_EXPIRED"} and not (
        _value(row, "closed_at") or _value(row, "settled_at")
    ):
        return "expired_unfilled"
    if status == "PAPER_SETTLED" or _value(row, "settled_at"):
        return "held_to_settlement"

    explicit = _explicit_exit_reason(_value(row, "outcome_diagnostics_json"))
    if explicit is not None:
        return explicit
    if status == "PAPER_CLOSED" or _value(row, "closed_at"):
        pnl = _finite(_value(row, "realized_pnl"))
        if pnl is None:
            return "unclassified"
        if abs(pnl) <= 1e-9:
            return "break_even"
        # Legacy close rows predate explicit monitor actions. Their immutable,
        # after-fee execution-lot P&L is the audited fallback used by the
        # historical Strategy Lab semantics.
        return "take_profit" if pnl > 0 else "stop_loss"
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
    text = " ".join(str(value or "").upper() for value in candidates)
    if "TAKE_PROFIT" in text or "TAKE PROFIT" in text:
        return "take_profit"
    if "STOP_LOSS" in text or "STOP LOSS" in text or "MODEL_VETO" in text:
        return "stop_loss"
    if "BREAK_EVEN" in text or "BREAK EVEN" in text:
        return "break_even"
    return None


def _value(row: Any, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None
