from __future__ import annotations

import math
import sqlite3
from functools import partial
from typing import Iterable

from .._util import _row_value as _shared_row_value
from ..settlement_truth import (
    integer_settlement_high_f as _integer_settlement_high_f,
    is_pre_resolution_decision as _is_pre_resolution_decision,
    normalize_settlement_truth,
    row_resolves_yes as _decision_row_resolves_yes,
    settlement_for_market,
)
from .diagnostics import _row_side

_row_value = partial(_shared_row_value, default_on_none=True)


def market_backtest_summary(
    self,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, float]:
    filters, params = _date_filters(since, until)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with self.connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM paper_orders {where}",
            params,
        ).fetchall()
    # PAPER_EXPIRED rows are resting limits that never filled; they carry
    # realized_pnl=0.0 but deployed no capital and resolved no position, so
    # they must not count as orders, dilute the capital/ROI denominator, or
    # drag the hit-rate denominator as phantom losses.
    realized_rows = [
        row
        for row in rows
        if row["realized_pnl"] is not None and row["status"] != "PAPER_EXPIRED"
    ]
    open_rows = [
        row
        for row in rows
        if row["status"] in {
            "PAPER_FILLED", "PAPER_PARTIALLY_FILLED", "PAPER_PARTIAL_EXPIRED"
        }
        and row["realized_pnl"] is None
    ]
    open_capital = sum(float(row["contracts"]) * float(row["cost_per_contract"]) for row in open_rows)
    if not realized_rows:
        return {
            "orders": 0,
            "contracts": 0.0,
            "capital_at_risk": 0.0,
            "realized_pnl": 0.0,
            "roi": 0.0,
            "hit_rate": 0.0,
            "wins": 0.0,
            "losses": 0.0,
            "avg_edge": 0.0,
            "open_orders": float(len(open_rows)),
            "open_capital_at_risk": open_capital,
        }
    contracts = sum(float(row["contracts"]) for row in realized_rows)
    capital = sum(float(row["contracts"]) * float(row["cost_per_contract"]) for row in realized_rows)
    pnl = sum(float(row["realized_pnl"]) for row in realized_rows)
    # A break-even close (resolved_yes NULL, realized_pnl 0) is undecided: it
    # deployed capital (so it stays in orders/contracts/capital/pnl) but is
    # neither a win nor a loss, so it must not drag the hit-rate denominator
    # the way the old realized_pnl > 0 fallback did.
    decided_rows = [row for row in realized_rows if _row_position_decided(row)]
    hits = sum(1 for row in decided_rows if _row_position_won(row))
    losses = len(decided_rows) - hits
    return {
        "orders": float(len(realized_rows)),
        "contracts": contracts,
        "capital_at_risk": capital,
        "realized_pnl": pnl,
        "roi": pnl / capital if capital else 0.0,
        # decided_rows excludes undecided break-evens; a 0-for-N losing
        # streak still reports 0.0 (hits=0 over a non-empty denominator).
        "hit_rate": hits / len(decided_rows) if decided_rows else 0.0,
        "wins": float(hits),
        "losses": float(losses),
        "avg_edge": sum(float(row["edge"]) for row in realized_rows) / len(realized_rows),
        "open_orders": float(len(open_rows)),
        "open_capital_at_risk": open_capital,
    }

def sampled_decision_rows(
    self,
    *,
    since: str | None = None,
    until: str | None = None,
    approved_only: bool = False,
    min_quality: float | None = None,
    pre_resolution_only: bool = True,
    sample_mode: str = "entry-per-market-side",
) -> list[sqlite3.Row]:
    """Read, pre-resolution-filter, and dedup decision snapshots.

    Shared read path for ``signal_backtest_summary`` and the config rescorer
    (``backtest_rescore``): both want the same look-ahead-free, deduped set
    of decision rows. The rescorer then re-decides each row under a candidate
    ``StrategyConfig`` instead of reading the recorded approval/size. Dedup
    runs on the persisted ``approved`` flag (the live scanner's verdict) so a
    candidate config does not change which snapshot is selected as the entry.
    """

    if sample_mode not in {"latest-per-market-side", "entry-per-market-side", "all"}:
        raise ValueError(
            "sample_mode must be latest-per-market-side, entry-per-market-side, or all"
        )
    filters, params = _date_filters(since, until)
    if approved_only:
        filters.append("approved = 1")
    if min_quality is not None:
        filters.append("trade_quality_score >= ?")
        params.append(str(float(min_quality)))
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with self.connect() as conn:
        conn.row_factory = sqlite3.Row
        if sample_mode != "all":
            pre_filter = (
                # Both values are canonical UTC ISO strings, so lexical
                # ordering preserves the pre-close instant comparison.
                "COALESCE(intraday_is_complete, 0) = 0 "
                "AND market_close_time IS NOT NULL AND created_at < market_close_time"
                if pre_resolution_only
                else "1 = 1"
            )
            ordering = (
                "approved DESC, created_at, id"
                if sample_mode == "entry-per-market-side"
                else "created_at DESC, id DESC"
            )
            ranked_where = f"{where} {'AND' if where else 'WHERE'} {pre_filter}"
            return conn.execute(
                f"""
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY target_date, market_ticker,
                               CASE
                                   WHEN UPPER(side) IN ('YES', 'NO') THEN UPPER(side)
                                   WHEN instr(UPPER(action), 'NO') > 0 THEN 'NO'
                                   ELSE 'YES'
                               END
                               ORDER BY {ordering}
                           ) AS sample_rank
                    FROM decision_snapshots
                    {ranked_where}
                )
                SELECT d.*
                FROM ranked r
                JOIN decision_snapshots d ON d.id = r.id
                WHERE r.sample_rank = 1
                ORDER BY d.target_date, d.created_at, d.id
                """,
                params,
            ).fetchall()
        # Stream the cursor instead of fetchall(): decision_snapshots grows
        # by thousands of ~7KB-JSON rows per day, and materializing every
        # row memory-thrashed the 1GB refresh box. Sampling keeps at most
        # one row per market-side, so a single pass is enough.
        cursor = conn.execute(
            f"SELECT * FROM decision_snapshots {where} ORDER BY target_date, created_at",
            params,
        )
        pre_resolution_rows = (
            row for row in cursor if not pre_resolution_only or _is_pre_resolution_decision(row)
        )
        return _sample_decision_rows(pre_resolution_rows, sample_mode)

def signal_backtest_summary(
    self,
    settlements: dict[object, float],
    *,
    since: str | None = None,
    until: str | None = None,
    approved_only: bool = False,
    min_quality: float | None = None,
    pre_resolution_only: bool = True,
    sample_mode: str = "latest-per-market-side",
    sampled_rows: list[sqlite3.Row] | None = None,
) -> dict[str, object]:
    if sample_mode not in {"latest-per-market-side", "entry-per-market-side", "all"}:
        raise ValueError(
            "sample_mode must be latest-per-market-side, entry-per-market-side, or all"
        )
    # Score signals against the integer Kalshi settlement, matching the live
    # settle path (settle_paper_orders) and backtest_rescore. Using the raw
    # fractional high here made win_rate/Brier/log_loss/calibration wrong on
    # every fractional-high day near a bin edge -- the exact numbers used to
    # judge model edge and gate profiles.
    normalized_settlements = {
        key: _integer_settlement_high_f(value)
        for key, value in normalize_settlement_truth(settlements).items()
    }
    filters, params = _date_filters(since, until)
    if approved_only:
        filters.append("approved = 1")
    if min_quality is not None:
        filters.append("trade_quality_score >= ?")
        params.append(str(float(min_quality)))
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    counts = {"raw": 0, "raw_approved": 0, "pre": 0, "pre_approved": 0}
    with self.connect() as conn:
        conn.row_factory = sqlite3.Row
        raw_count_row = conn.execute(
            f"""
            SELECT COUNT(*) AS raw,
                   COALESCE(SUM(CASE WHEN approved=1 THEN 1 ELSE 0 END), 0) AS raw_approved
            FROM decision_snapshots {where}
            """,
            params,
        ).fetchone()
        pre_where = (
            f"{where} {'AND' if where else 'WHERE'} "
            "COALESCE(intraday_is_complete,0)=0 "
            "AND market_close_time IS NOT NULL AND created_at < market_close_time"
        )
        pre_count_row = conn.execute(
            f"""
            SELECT COUNT(*) AS pre,
                   COALESCE(SUM(CASE WHEN approved=1 THEN 1 ELSE 0 END), 0) AS pre_approved
            FROM decision_snapshots {pre_where}
            """,
            params,
        ).fetchone()
        counts = {
            "raw": int(raw_count_row["raw"] or 0),
            "raw_approved": int(raw_count_row["raw_approved"] or 0),
            "pre": int(pre_count_row["pre"] or 0),
            "pre_approved": int(pre_count_row["pre_approved"] or 0),
        }
    if sampled_rows is None:
        sampled_rows = self.sampled_decision_rows(
            since=since,
            until=until,
            approved_only=approved_only,
            min_quality=min_quality,
            pre_resolution_only=pre_resolution_only,
            sample_mode=sample_mode,
        )
    if not pre_resolution_only:
        counts["pre"] = counts["raw"]
        counts["pre_approved"] = counts["raw_approved"]
    settled_rows = [
        row
        for row in sampled_rows
        if settlement_for_market(
            normalized_settlements, str(row["market_ticker"]), row["target_date"]
        )
        is not None
    ]
    if not settled_rows:
        return {
            "signals": float(len(sampled_rows)),
            "raw_signals": float(counts["raw"]),
            "pre_resolution_signals": float(counts["pre"]),
            "settled_signals": 0.0,
            "approved_signals": float(sum(1 for row in sampled_rows if int(row["approved"]))),
            "approved_raw_signals": float(counts["raw_approved"]),
            "approved_pre_resolution_signals": float(counts["pre_approved"]),
            "approval_rate": _safe_div(sum(1 for row in sampled_rows if int(row["approved"])), len(sampled_rows)),
            "excluded_post_resolution_signals": float(counts["raw"] - counts["pre"]),
            "sample_mode": sample_mode,
            "pre_resolution_only": pre_resolution_only,
            "brier_score": 0.0,
            "log_loss": 0.0,
            "win_rate": 0.0,
            "avg_probability": 0.0,
            "avg_edge": 0.0,
            "avg_edge_lcb": 0.0,
            "avg_quality": 0.0,
            "approved_paper_pnl": 0.0,
            "approved_capital_at_risk": 0.0,
            "approved_roi": 0.0,
            "approved_hit_rate": 0.0,
            "quality_buckets": [],
            "probability_streams": {},
        }

    outcomes = []
    for row in settled_rows:
        settlement = settlement_for_market(
            normalized_settlements, str(row["market_ticker"]), row["target_date"]
        )
        if settlement is None:  # guarded by settled_rows; keeps typing honest
            continue
        position_won = _decision_row_position_won(row, settlement)
        probability = float(row["probability"])
        outcomes.append((row, 1.0 if position_won else 0.0, probability))

    approved = [(row, outcome, probability) for row, outcome, probability in outcomes if int(row["approved"])]
    capital = sum(float(row["recommended_spend"]) for row, _, _ in approved)
    pnl = sum(_decision_row_pnl(row, bool(outcome)) for row, outcome, _ in approved)
    return {
        "signals": float(len(sampled_rows)),
        "raw_signals": float(counts["raw"]),
        "pre_resolution_signals": float(counts["pre"]),
        "settled_signals": float(len(settled_rows)),
        "approved_signals": float(sum(1 for row in sampled_rows if int(row["approved"]))),
        "approved_raw_signals": float(counts["raw_approved"]),
        "approved_pre_resolution_signals": float(counts["pre_approved"]),
        "approval_rate": _safe_div(sum(1 for row in sampled_rows if int(row["approved"])), len(sampled_rows)),
        "excluded_post_resolution_signals": float(counts["raw"] - counts["pre"]),
        "sample_mode": sample_mode,
        "pre_resolution_only": pre_resolution_only,
        "brier_score": sum((probability - outcome) ** 2 for _, outcome, probability in outcomes) / len(outcomes),
        "log_loss": sum(
            -math.log(max(1e-12, probability if outcome else 1.0 - probability))
            for _, outcome, probability in outcomes
        )
        / len(outcomes),
        "win_rate": sum(outcome for _, outcome, _ in outcomes) / len(outcomes),
        "avg_probability": sum(probability for _, _, probability in outcomes) / len(outcomes),
        "avg_edge": sum(float(row["edge"]) for row, _, _ in outcomes) / len(outcomes),
        "avg_edge_lcb": sum(float(row["edge_lcb"]) for row, _, _ in outcomes) / len(outcomes),
        "avg_quality": sum(float(row["trade_quality_score"]) for row, _, _ in outcomes) / len(outcomes),
        "approved_paper_pnl": pnl,
        "approved_capital_at_risk": capital,
        "approved_roi": pnl / capital if capital else 0.0,
        "approved_hit_rate": _safe_div(sum(outcome for _, outcome, _ in approved), len(approved)),
        "quality_buckets": _quality_buckets(outcomes),
        "probability_streams": _probability_stream_metrics(outcomes),
    }

def _date_filters(since: str | None, until: str | None) -> tuple[list[str], list[str]]:
    filters: list[str] = []
    params: list[str] = []
    if since:
        filters.append("target_date >= ?")
        params.append(since)
    if until:
        filters.append("target_date <= ?")
        params.append(until)
    return filters, params


def _sample_decision_rows(rows: Iterable[sqlite3.Row], sample_mode: str) -> list[sqlite3.Row]:
    if sample_mode == "all":
        return list(rows)
    if sample_mode == "entry-per-market-side":
        return _entry_decision_rows(rows)
    latest = {}
    for row in rows:
        key = (str(row["target_date"]), str(row["market_ticker"]), _row_side(row))
        current = latest.get(key)
        if current is None or _row_sort_time(row) >= _row_sort_time(current):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: (str(row["target_date"]), str(row["created_at"]), int(row["id"])))


def _entry_decision_rows(rows: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    """First approved snapshot per market/side — the decision that opened the
    position — falling back to the first snapshot when nothing was approved.

    The latest pre-resolution snapshot can look very different from the entry
    the scanner actually traded, so backtests of the entry decision must not
    sample it.
    """

    entries: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["target_date"]), str(row["market_ticker"]), _row_side(row))
        current = entries.get(key)
        if current is None:
            entries[key] = row
            continue
        row_rank = (0 if int(row["approved"]) else 1, _row_sort_time(row))
        current_rank = (0 if int(current["approved"]) else 1, _row_sort_time(current))
        if row_rank < current_rank:
            entries[key] = row
    return sorted(entries.values(), key=lambda row: (str(row["target_date"]), str(row["created_at"]), int(row["id"])))


def _row_sort_time(row: sqlite3.Row) -> tuple[str, int]:
    return (str(row["created_at"]), int(row["id"]))


def _row_position_won(row: sqlite3.Row) -> bool:
    try:
        resolved_yes = row["resolved_yes"]
    except (IndexError, KeyError):
        resolved_yes = None
    if resolved_yes is None:
        return float(row["realized_pnl"] or 0.0) > 0.0
    side = _row_side(row)
    return bool(resolved_yes) if side == "YES" else not bool(resolved_yes)


def _row_position_decided(row: sqlite3.Row) -> bool:
    """Whether a realized row has a decided win/loss outcome.

    A break-even early close stores ``resolved_yes = NULL`` with
    ``realized_pnl == 0`` (see ``close_paper_order``); it is genuinely undecided
    and must be excluded from the hit-rate denominator rather than counted as a
    loss. Any row with a recorded ``resolved_yes`` (settled, or a decided close)
    is decided; a NULL-``resolved_yes`` legacy row is decided iff its PnL is
    non-zero (its win/loss can still be read from the PnL sign).
    """

    try:
        resolved_yes = row["resolved_yes"]
    except (IndexError, KeyError):
        resolved_yes = None
    if resolved_yes is not None:
        return True
    return abs(float(row["realized_pnl"] or 0.0)) > 1e-9


def _decision_row_position_won(row: sqlite3.Row, settlement_high_f: float) -> bool:
    resolved_yes = _decision_row_resolves_yes(row, settlement_high_f)
    side = str(row["side"]).upper()
    return resolved_yes if side == "YES" else not resolved_yes


def _decision_row_pnl(row: sqlite3.Row, position_won: bool) -> float:
    contracts = float(row["recommended_contracts"])
    cost = float(row["cost_per_contract"])
    return contracts * ((1.0 - cost) if position_won else -cost)


def _probability_stream_metrics(
    rows: list[tuple[sqlite3.Row, float, float]],
) -> dict[str, dict[str, float]]:
    """Score the weather model, market prior, and traded posterior separately.

    The traded probability blends the market prior in, so its calibration can
    look good even when the weather model adds nothing. Comparing the streams
    on the same settled rows shows whether the model is real alpha or just
    market agreement.
    """

    streams = {
        "traded": lambda row: float(row["probability"]),
        "weather_model": lambda row: _row_optional_float(row, "model_probability"),
        "market_prior": lambda row: _row_optional_float(row, "market_probability"),
    }
    output: dict[str, dict[str, float]] = {}
    for name, extract in streams.items():
        scored = [
            (outcome, probability)
            for row, outcome, _ in rows
            if (probability := extract(row)) is not None
        ]
        if not scored:
            continue
        output[name] = {
            "settled": float(len(scored)),
            "brier_score": sum((probability - outcome) ** 2 for outcome, probability in scored)
            / len(scored),
            "log_loss": sum(
                -math.log(max(1e-12, probability if outcome else 1.0 - probability))
                for outcome, probability in scored
            )
            / len(scored),
            "avg_probability": sum(probability for _, probability in scored) / len(scored),
            "win_rate": sum(outcome for outcome, _ in scored) / len(scored),
        }
    return output


def _row_optional_float(row: sqlite3.Row, key: str) -> float | None:
    value = _row_value(row, key)
    return None if value is None else float(value)


def _quality_buckets(rows: list[tuple[sqlite3.Row, float, float]]) -> list[dict[str, float]]:
    buckets = [
        ("0-20", 0.0, 20.0),
        ("20-40", 20.0, 40.0),
        ("40-60", 40.0, 60.0),
        ("60-80", 60.0, 80.0),
        ("80-100", 80.0, 100.000001),
    ]
    output: list[dict[str, float]] = []
    for label, lower, upper in buckets:
        bucket = [
            (row, outcome, probability)
            for row, outcome, probability in rows
            if lower <= float(row["trade_quality_score"]) < upper
        ]
        if not bucket:
            continue
        approved = [item for item in bucket if int(item[0]["approved"])]
        capital = sum(float(row["recommended_spend"]) for row, _, _ in approved)
        pnl = sum(_decision_row_pnl(row, bool(outcome)) for row, outcome, _ in approved)
        output.append(
            {
                "range": label,
                "count": float(len(bucket)),
                "approved": float(len(approved)),
                "avg_probability": sum(probability for _, _, probability in bucket) / len(bucket),
                "win_rate": sum(outcome for _, outcome, _ in bucket) / len(bucket),
                "brier_score": sum((probability - outcome) ** 2 for _, outcome, probability in bucket) / len(bucket),
                "approved_pnl": pnl,
                "approved_roi": pnl / capital if capital else 0.0,
            }
        )
    return output


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0
