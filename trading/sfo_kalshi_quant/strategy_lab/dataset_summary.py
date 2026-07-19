from __future__ import annotations

from typing import Any

from .._util import _to_float
from ..dataset_research import DEFAULT_MIN_AFTER_COST_TRADES, DEFAULT_MIN_MATCHED_ROWS


def _dataset_research_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Compact Strategy Lab view of the daily dataset-research verdict.

    The backfill timer collects IEM/Open-Meteo/Kalshi history every night; this
    block is what proves the collection is feeding the promotion decision
    rather than sitting unused in the DB.
    """

    if not payload:
        return {
            "available": False,
            "reason": "dataset_research.json not published yet; the nightly backfill writes it after collection.",
        }
    # dataset_research.py publishes candidates under accuracy_gate with MAE
    # metrics nested in each row's holdout block (the decision basis); legacy
    # top-level payloads are kept readable as a fallback.
    accuracy_gate = payload.get("accuracy_gate")
    if not isinstance(accuracy_gate, dict):
        accuracy_gate = {}
    candidates = accuracy_gate.get("candidates", payload.get("candidates"))
    candidate_rows = candidates if isinstance(candidates, list) else []
    top = [
        _dataset_candidate_row(row)
        for row in candidate_rows[:6]
        if isinstance(row, dict)
    ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    candidate_count = _dataset_candidate_count(
        accuracy_gate=accuracy_gate,
        payload=payload,
        candidate_rows=candidate_rows,
    )
    accuracy_candidates = _dataset_accuracy_candidate_count(
        accuracy_gate=accuracy_gate,
        payload=payload,
        candidate_rows=candidate_rows,
    )
    minimum_matched_rows = int(
        _to_float(accuracy_gate.get("minimum_matched_rows"), DEFAULT_MIN_MATCHED_ROWS)
    )
    profitability_gate = payload.get("profitability_gate")
    if not isinstance(profitability_gate, dict):
        profitability_gate = {}
    dataset_stack = _dataset_stack_summary(
        payload.get("dataset_stack") if isinstance(payload.get("dataset_stack"), dict) else {},
        candidate_rows=candidate_rows,
        minimum_matched_rows=minimum_matched_rows,
    )
    dataset_coverage = payload.get("dataset_coverage")
    if not isinstance(dataset_coverage, dict):
        dataset_coverage = {}
    headline = summary.get("headline") or _dataset_research_headline(
        candidate_count=candidate_count,
        accuracy_candidate_count=accuracy_candidates,
        candidate_rows=candidate_rows,
    )
    blocking_gates = summary.get("blocking_gates") or _dataset_blocking_gates(
        candidate_count=candidate_count,
        accuracy_candidate_count=accuracy_candidates,
        candidate_rows=candidate_rows,
        minimum_matched_rows=minimum_matched_rows,
        profitability_gate=profitability_gate,
    )
    action_items = summary.get("action_items") or _dataset_action_items(
        candidate_count=candidate_count,
        candidate_rows=candidate_rows,
        minimum_matched_rows=minimum_matched_rows,
        profitability_gate=profitability_gate,
    )
    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "status": payload.get("status"),
        "headline": headline,
        "promotion_rule": payload.get("promotion_rule") or _dataset_promotion_rule(),
        "candidate_count": candidate_count,
        "accuracy_candidate_count": accuracy_candidates,
        "blocking_gates": blocking_gates,
        "action_items": action_items,
        "baseline": payload.get("baseline") or {},
        "dataset_coverage": dataset_coverage,
        "dataset_stack": dataset_stack,
        "profitability_gate": profitability_gate,
        "candidates": top,
    }


def _dataset_candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    holdout = row.get("holdout")
    metrics = holdout if isinstance(holdout, dict) else row
    return {
        "dataset_key": row.get("dataset_key"),
        "decision": row.get("decision"),
        "reason": row.get("reason"),
        "next_use": row.get("next_use")
        or _dataset_candidate_next_use(row.get("decision"), row.get("reason")),
        "matched_rows": row.get("matched_rows"),
        "dataset_mae_f": metrics.get("dataset_mae_f"),
        "baseline_mae_f": metrics.get("baseline_mae_f"),
        "mae_delta_vs_baseline_f": metrics.get("mae_delta_vs_baseline_f"),
    }


def _dataset_candidate_count(
    *,
    accuracy_gate: dict[str, Any],
    payload: dict[str, Any],
    candidate_rows: list[Any],
) -> int:
    value = accuracy_gate.get("candidate_count", payload.get("candidate_count"))
    if value is None:
        return len(candidate_rows)
    return int(_to_float(value))


def _dataset_accuracy_candidate_count(
    *,
    accuracy_gate: dict[str, Any],
    payload: dict[str, Any],
    candidate_rows: list[Any],
) -> int:
    value = accuracy_gate.get("accuracy_candidate_count", payload.get("accuracy_candidate_count"))
    if value is not None:
        return int(_to_float(value))
    return sum(
        1
        for row in candidate_rows
        if isinstance(row, dict) and row.get("decision") == "accuracy_candidate"
    )


def _dataset_stack_summary(
    stack: dict[str, Any],
    *,
    candidate_rows: list[Any],
    minimum_matched_rows: int,
) -> dict[str, Any]:
    if stack:
        return stack
    max_matched = _dataset_max_matched_rows(candidate_rows)
    return {
        "available": False,
        "decision": "collect_only",
        "reason": (
            "Combined dataset stack is waiting for matched settlement rows; "
            f"best published feature has {max_matched} and needs {minimum_matched_rows}."
        ),
        "minimum_matched_rows": minimum_matched_rows,
        "max_matched_rows": max_matched,
        "feature_keys": [],
    }


def _dataset_research_headline(
    *,
    candidate_count: int,
    accuracy_candidate_count: int,
    candidate_rows: list[Any],
) -> str:
    max_matched = _dataset_max_matched_rows(candidate_rows)
    if accuracy_candidate_count > 0:
        return "Some dataset views show forecast lift, but trade weighting is still gated."
    if candidate_count <= 0:
        return "Dataset research is waiting for backfilled forecast feature rows."
    if max_matched <= 0:
        return "Dataset collection is live, but forecast features are not yet matched to settled SFO highs."
    return "Dataset collection is live; no source has cleared the holdout improvement gate yet."


def _dataset_blocking_gates(
    *,
    candidate_count: int,
    accuracy_candidate_count: int,
    candidate_rows: list[Any],
    minimum_matched_rows: int,
    profitability_gate: dict[str, Any],
) -> list[str]:
    gates = []
    max_matched = _dataset_max_matched_rows(candidate_rows)
    if candidate_count <= 0:
        gates.append("dataset gate: no forecast feature candidates published yet")
    if max_matched < minimum_matched_rows:
        gates.append(
            f"accuracy gate: best dataset feature has {max_matched} matched settlement rows; "
            f"needs {minimum_matched_rows}"
        )
    elif accuracy_candidate_count <= 0:
        gates.append("accuracy gate: no dataset feature has beaten LSTM on holdout yet")

    market_history = profitability_gate.get("market_history")
    trade_rows = 0
    if isinstance(market_history, dict):
        trade_rows = int(_to_float(market_history.get("trades")))
    minimum_trades = int(
        _to_float(profitability_gate.get("minimum_after_cost_trades"), DEFAULT_MIN_AFTER_COST_TRADES)
    )
    if trade_rows < minimum_trades:
        gates.append(f"market gate: {trade_rows} after-cost trade rows; needs {minimum_trades}")
    return gates


def _dataset_action_items(
    *,
    candidate_count: int,
    candidate_rows: list[Any],
    minimum_matched_rows: int,
    profitability_gate: dict[str, Any],
) -> list[str]:
    items = []
    max_matched = _dataset_max_matched_rows(candidate_rows)
    if candidate_count <= 0:
        items.append("Run the compact dataset backfill so forecast feature candidates publish into Strategy Lab.")
    if max_matched < minimum_matched_rows:
        items.append(
            f"Keep nightly dataset backfill running until each candidate has {minimum_matched_rows} "
            "matched settled highs."
        )
    market_history = profitability_gate.get("market_history")
    trade_rows = 0
    if isinstance(market_history, dict):
        trade_rows = int(_to_float(market_history.get("trades")))
    minimum_trades = int(
        _to_float(profitability_gate.get("minimum_after_cost_trades"), DEFAULT_MIN_AFTER_COST_TRADES)
    )
    if trade_rows < minimum_trades:
        items.append("Backfill or enable prediction-market trade history before using datasets for trading weight.")
    items.append("Keep LSTM as the live model until both accuracy and market gates pass.")
    return items


def _dataset_max_matched_rows(candidate_rows: list[Any]) -> int:
    return max(
        (
            int(_to_float(row.get("matched_rows")))
            for row in candidate_rows
            if isinstance(row, dict)
        ),
        default=0,
    )


def _dataset_candidate_next_use(decision: Any, reason: Any) -> str:
    if decision == "accuracy_candidate":
        return (
            "Eligible for challenger-model research; keep live trading weight at zero "
            "until the after-cost market gate passes."
        )
    if reason:
        return f"Keep collecting; {reason}"
    return "Keep collecting until the accuracy and market gates pass."


def _dataset_promotion_rule() -> str:
    return (
        "Collect broadly, but do not give a new source live model weight or loosen "
        "paper-trading gates until it improves held-out forecast error and then "
        "survives an after-cost market backtest with enough trades."
    )
