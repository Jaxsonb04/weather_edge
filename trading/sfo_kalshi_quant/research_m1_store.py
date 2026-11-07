"""M1 market-recalibration paper trial: scan engine, admission and evidence.

The research-profile portfolio scan hands every day-ahead leg it evaluated to
:func:`run_m1_scan`. The engine applies the frozen rule from
``research_m1.py``, records the first research scan of each UTC hour for every
leg in M1's own durable tables, keeps the two shadow arms online, and -- only
when placement is enabled -- admits entries atomically into the separate
``paper-research-m1-v1`` paper ledger.

Isolation is structural: every read and write here is scoped to the M1
account or to M1's own tables, so no cash, P&L or exposure crosses into Live
Stability, Research V6 or Research V7, and no other account's gate changes.
Nothing here can reach real money: the module never imports
``live_execution`` and books only paper fills.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .cities import CITIES, CityConfig, city_for_market_ticker
from .db import PaperStore
from .maker_fills import EXECUTION_MODEL_VERSION
from .models import MarketBin, TradeDecision
from .research_m1 import (
    ALPHA_AGREE,
    ALPHA_AGREE_MINUS_DISAGREE,
    ALPHA_PRIMARY,
    ARM_FIVE_MINUTE,
    ARM_MODEL_VETO,
    DECISION_ACCOUNT_BLOCKED,
    DECISION_ADMISSION_ERROR,
    DECISION_ALREADY_CROSSED,
    DECISION_ALREADY_ENTERED,
    DECISION_ENTERED,
    DECISION_HALTED,
    DECISION_PENDING,
    DECISION_SHADOW,
    M1_ACCOUNT_ID,
    M1_FILL_MODEL,
    M1_HALT_EVENT,
    M1_LEAD_BUCKET,
    M1_POLICY,
    M1_PUBLISHED_PROFILE,
    SHADOW_ARMS,
    STOP_FIDELITY,
    STOP_FINAL_READ,
    STOP_FUTILITY,
    STOP_LOSS,
    STOP_PREMISE,
    STOP_RULES,
    ActualEntry,
    M1LegEvaluation,
    M1LegInputs,
    PremiseRow,
    RecordedEvaluation,
    SettledEntry,
    civil_lead_days,
    date_clustered_bootstrap,
    date_clustered_difference_bootstrap,
    evaluate_leg,
    match_entries,
    order_fee,
    parse_timestamp,
    policy_summary,
    premise_slope,
    replay_expected_entries,
    resolves_yes,
    roi,
)
from .research_policy import RESEARCH_OBJECTIVE_TZ
from .store.diagnostics import _market_close_time

# The westernmost station clock. A target date's last day-ahead hour ends at
# midnight here, so the trading-day horizon closes then.
_LATEST_STATION_TZ = ZoneInfo("America/Los_Angeles")


_OPEN_STATUSES = ("PAPER_FILLED", "PAPER_PARTIALLY_FILLED", "PAPER_PARTIAL_EXPIRED")
_EPS = 1e-9
# The settle timer reconciles at most this many UTC days per run, oldest first,
# so a long outage catches up without turning one tick into a history backfill.
RECONCILIATION_LOOKBACK_DAYS = 14
# Publication reads are descriptive; the final read uses the pre-registered
# 10,000 resamples (research_m1.FINAL_READ_BOOTSTRAP_RESAMPLES).
PUBLICATION_BOOTSTRAP_RESAMPLES = 2_000


@dataclass
class M1ScanResult:
    target_date: str
    scan_id: str
    skipped_reason: str | None = None
    legs: int = 0
    sampled: int = 0
    candidates: int = 0
    shadow_entries: dict[str, int] = field(default_factory=dict)
    order_ids: list[int] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)


def _utc_now(store: PaperStore, now: datetime | None) -> datetime:
    value = now if now is not None else store._research_clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("M1 clock must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------
# Inputs from one research scan context
# --------------------------------------------------------------------------


def build_leg_inputs(
    decisions: Sequence[TradeDecision],
    *,
    created_at: datetime,
    target_date: date,
    city: CityConfig,
    markets: Sequence[MarketBin],
    forecast_age_hours: float | None,
    source_spread_f: float | None,
    entry_gate_reason: str | None,
) -> list[tuple[M1LegInputs, TradeDecision]]:
    """Turn the scan's per-leg decisions into the rule's recorded inputs."""

    markets_by_ticker = {market.ticker: market for market in markets}
    rows: list[tuple[M1LegInputs, TradeDecision]] = []
    for decision in decisions:
        side = str(decision.side or "YES").upper()
        market = markets_by_ticker.get(decision.ticker)
        rows.append(
            (
                M1LegInputs(
                    created_at=created_at,
                    target_date=target_date,
                    market_ticker=str(decision.ticker),
                    side=side,
                    civil_tz=city.civil_tz_name,
                    market_probability=_float_or_none(decision.market_probability),
                    model_probability=_float_or_none(decision.model_probability),
                    ask=_float_or_none(decision.ask),
                    bid=_float_or_none(decision.bid),
                    ask_size=_float_or_none(decision.ask_size),
                    bid_size=_float_or_none(decision.bid_size),
                    market_status=(market.status if market is not None else None),
                    market_close_time=(
                        _market_close_time(market.raw) if market is not None else None
                    ),
                    forecast_age_hours=forecast_age_hours,
                    source_spread_f=source_spread_f,
                    entry_gate_reason=entry_gate_reason,
                ),
                decision,
            )
        )
    return rows


# --------------------------------------------------------------------------
# Durable reads
# --------------------------------------------------------------------------


def m1_halts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT created_at, idempotency_key, details_json FROM paper_account_ledger "
        "WHERE account_id=? AND event_type=? ORDER BY id",
        (M1_ACCOUNT_ID, M1_HALT_EVENT),
    ).fetchall()
    halts = []
    for created_at, key, details_json in rows:
        try:
            details = json.loads(details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        halts.append(
            {
                "rule": str(details.get("rule") or str(key).rsplit(":", 1)[-1]),
                "recorded_at": created_at,
                "details": details,
            }
        )
    return halts


def record_m1_halt(
    conn: sqlite3.Connection,
    rule: str,
    *,
    reason: str,
    metrics: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> bool:
    """Record one stop rule as an immutable, zero-amount account event.

    Halts stop new entries only. They never close positions and nothing in the
    code removes them, so there is no automatic resumption.
    """

    if rule not in STOP_RULES:
        raise ValueError(f"unknown M1 stop rule: {rule}")
    created_at = _iso(now or datetime.now(UTC))
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_account_ledger "
        "(created_at, account_id, order_id, event_type, amount, idempotency_key, "
        "details_json) VALUES (?, ?, NULL, ?, 0.0, ?, ?)",
        (
            created_at,
            M1_ACCOUNT_ID,
            M1_HALT_EVENT,
            f"{M1_ACCOUNT_ID}:halt:{rule}",
            json.dumps(
                {
                    "rule": rule,
                    "reason": reason,
                    "metrics": dict(metrics or {}),
                    "policy_fingerprint": M1_POLICY.policy_fingerprint,
                    "policy_version": M1_POLICY.policy_version,
                    "effect": "new entries halted; open positions held to settlement",
                    "automatic_resumption": False,
                },
                sort_keys=True,
                default=str,
            ),
        ),
    )
    return cursor.rowcount == 1


def _entered_legs(
    conn: sqlite3.Connection, *, before: str | None = None
) -> set[tuple[str, str, str]]:
    query = (
        "SELECT target_date, market_ticker, UPPER(COALESCE(side, 'YES')) "
        "FROM paper_orders WHERE account_id=? AND parent_order_id IS NULL "
        "AND status != 'REJECTED'"
    )
    params: tuple[object, ...] = (M1_ACCOUNT_ID,)
    if before is not None:
        query += " AND created_at < ?"
        params = (*params, before)
    return {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(query, params).fetchall()
    }


def _shadow_crossed_legs(
    conn: sqlite3.Connection,
    *,
    target_date: str | None = None,
    before: str | None = None,
) -> set[tuple[str, str, str]]:
    """Legs whose first passing sampled evaluation was recorded in shadow mode."""

    query = (
        "SELECT DISTINCT target_date, market_ticker, side FROM m1_evaluations "
        "WHERE leg_passes = 1 AND placement_enabled = 0"
    )
    params: tuple[object, ...] = ()
    if target_date is not None:
        query += " AND target_date = ?"
        params = (*params, target_date)
    if before is not None:
        query += " AND created_at < ?"
        params = (*params, before)
    return {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(query, params).fetchall()
    }


def _entry_counts(conn: sqlite3.Connection) -> tuple[int, set[str]]:
    rows = conn.execute(
        "SELECT target_date FROM paper_orders WHERE account_id=? "
        "AND parent_order_id IS NULL AND status != 'REJECTED'",
        (M1_ACCOUNT_ID,),
    ).fetchall()
    return len(rows), {str(row[0]) for row in rows}


def _realized_pnl(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_orders "
        "WHERE account_id=? AND realized_pnl IS NOT NULL "
        "AND status NOT IN ('REJECTED', 'PAPER_EXPIRED')",
        (M1_ACCOUNT_ID,),
    ).fetchone()
    return float(row[0] or 0.0)


def _objective_day_bounds(day: date) -> tuple[str, str]:
    lower = datetime.combine(day, time.min, tzinfo=RESEARCH_OBJECTIVE_TZ)
    upper = datetime.combine(day + timedelta(days=1), time.min, tzinfo=RESEARCH_OBJECTIVE_TZ)
    return _iso(lower), _iso(upper)


def _daily_realized_pnl(conn: sqlite3.Connection, day: date) -> float:
    lower, upper = _objective_day_bounds(day)
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_orders "
        "WHERE account_id=? AND realized_pnl IS NOT NULL "
        "AND status NOT IN ('REJECTED', 'PAPER_EXPIRED') "
        "AND COALESCE(closed_at, settled_at) >= ? AND COALESCE(closed_at, settled_at) < ?",
        (M1_ACCOUNT_ID, lower, upper),
    ).fetchone()
    return float(row[0] or 0.0)


def _settled_entries(conn: sqlite3.Connection) -> list[SettledEntry]:
    rows = conn.execute(
        """
        SELECT o.target_date, o.realized_pnl, o.contracts, o.cost_per_contract,
               (SELECT e.agree FROM m1_evaluations e WHERE e.order_id = o.id
                ORDER BY e.id LIMIT 1) AS agree
        FROM paper_orders o
        WHERE o.account_id=? AND o.parent_order_id IS NULL
          AND o.realized_pnl IS NOT NULL
          AND o.status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
        ORDER BY o.id
        """,
        (M1_ACCOUNT_ID,),
    ).fetchall()
    return [
        SettledEntry(
            target_date=str(row[0]),
            pnl=float(row[1]),
            stake=float(row[2]) * float(row[3]),
            agree=None if row[4] is None else bool(row[4]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# Scan engine
# --------------------------------------------------------------------------


def _evaluation_row(
    evaluation: M1LegEvaluation,
    decision: TradeDecision,
    *,
    scan_id: str,
    placement_enabled: bool,
    recorded_decision: str,
    reason: str | None,
) -> tuple[object, ...]:
    inputs = evaluation.inputs
    return (
        _iso(inputs.created_at),
        evaluation.utc_hour,
        scan_id,
        M1_POLICY.policy_version,
        M1_POLICY.policy_fingerprint,
        M1_POLICY.coefficient_set_id,
        inputs.target_date.isoformat(),
        inputs.market_ticker,
        inputs.event_ticker,
        inputs.series_ticker,
        inputs.side,
        decision.strike_type,
        _float_or_none(decision.floor_strike),
        _float_or_none(decision.cap_strike),
        inputs.civil_tz,
        evaluation.local_hour,
        evaluation.lead_days,
        1 if evaluation.listing_hour else 0,
        evaluation.bucket,
        inputs.market_probability,
        evaluation.q_yes,
        evaluation.p_yes,
        evaluation.p_trade,
        inputs.model_probability,
        inputs.ask,
        inputs.bid,
        inputs.ask_size,
        inputs.bid_size,
        evaluation.spread_cents,
        evaluation.gate_fee,
        evaluation.cost,
        evaluation.edge,
        None if evaluation.agree is None else (1 if evaluation.agree else 0),
        evaluation.model_market_gap,
        inputs.market_status,
        inputs.market_close_time,
        _float_or_none(inputs.forecast_age_hours),
        _float_or_none(inputs.source_spread_f),
        inputs.entry_gate_reason,
        1 if evaluation.passes else 0,
        evaluation.contracts,
        evaluation.stake,
        1 if placement_enabled else 0,
        recorded_decision,
        reason,
    )


_EVALUATION_INSERT = """
INSERT OR IGNORE INTO m1_evaluations (
    created_at, utc_hour, scan_id, policy_version, policy_fingerprint,
    coefficient_set_id, target_date, market_ticker, event_ticker, series_ticker,
    side, strike_type, floor_strike, cap_strike, civil_tz, local_hour, lead_days,
    listing_hour, bucket, market_probability, q_yes, p_yes, p_trade,
    model_probability, ask, bid, ask_size, bid_size, spread_cents, gate_fee, cost,
    edge, agree, model_market_gap, market_status, market_close_time,
    forecast_age_hours, source_spread_f, entry_gate_reason, leg_passes, contracts,
    stake, placement_enabled, decision, reason
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
"""


def _record_shadow_entry(
    conn: sqlite3.Connection,
    arm: str,
    evaluation: M1LegEvaluation,
    decision: TradeDecision,
    *,
    scan_id: str,
) -> bool:
    inputs = evaluation.inputs
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO m1_shadow_entries (
            arm, created_at, utc_hour, scan_id, policy_fingerprint,
            coefficient_set_id, target_date, market_ticker, series_ticker, side,
            strike_type, floor_strike, cap_strike, bucket, q_yes, p_trade, cost,
            edge, ask, bid, ask_size, model_probability, agree, contracts, stake
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            arm,
            _iso(inputs.created_at),
            evaluation.utc_hour,
            scan_id,
            M1_POLICY.policy_fingerprint,
            M1_POLICY.coefficient_set_id,
            inputs.target_date.isoformat(),
            inputs.market_ticker,
            inputs.series_ticker,
            inputs.side,
            decision.strike_type,
            _float_or_none(decision.floor_strike),
            _float_or_none(decision.cap_strike),
            evaluation.bucket,
            evaluation.q_yes,
            evaluation.p_trade,
            evaluation.cost,
            evaluation.edge,
            inputs.ask,
            inputs.bid,
            inputs.ask_size,
            inputs.model_probability,
            None if evaluation.agree is None else (1 if evaluation.agree else 0),
            evaluation.contracts,
            evaluation.stake,
        ),
    )
    return cursor.rowcount == 1


def run_m1_scan(
    store: PaperStore,
    *,
    target_date: date,
    city: CityConfig,
    decisions: Sequence[TradeDecision],
    markets: Sequence[MarketBin],
    forecast: Any,
    entry_gate_reason: str | None,
    place: bool,
    now: datetime | None = None,
    scan_id: str | None = None,
) -> M1ScanResult:
    """Evaluate one research scan context for the M1 trial.

    Every scan feeds the five-minute shadow arm. The first research scan of
    each UTC hour that evaluates a leg is that leg's sample: the database's
    unique (leg, UTC hour) key decides it. Sampled legs are recorded with their
    decision, feed the model-veto shadow arm, and -- with placement enabled --
    are admitted atomically. With placement disabled every sampled decision is
    recorded in shadow mode and nothing is placed.
    """

    current = _utc_now(store, now)
    run_id = scan_id or f"m1-{uuid.uuid4().hex[:16]}"
    result = M1ScanResult(target_date=target_date.isoformat(), scan_id=run_id)
    if civil_lead_days(target_date, current, city.civil_tz_name) != M1_POLICY.lead_days:
        result.skipped_reason = "target is not the next civil day at the station"
        return result
    forecast_age = None
    source_spread = None
    if forecast is not None:
        try:
            forecast_age = forecast.age_hours(current)
        except (AttributeError, TypeError, ValueError):
            forecast_age = None
        source_spread = _float_or_none(getattr(forecast, "source_spread_f", None))
    pairs = build_leg_inputs(
        decisions,
        created_at=current,
        target_date=target_date,
        city=city,
        markets=markets,
        forecast_age_hours=forecast_age,
        source_spread_f=source_spread,
        entry_gate_reason=entry_gate_reason,
    )
    result.legs = len(pairs)
    if not pairs:
        result.skipped_reason = "no day-ahead legs in this scan"
        return result
    evaluated = [(evaluate_leg(inputs), decision) for inputs, decision in pairs]

    candidates: list[tuple[int, M1LegEvaluation, TradeDecision]] = []
    shadow_counts = {arm: 0 for arm in SHADOW_ARMS}
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        halts = m1_halts(conn)
        entered = _entered_legs(conn)
        shadow_crossed = _shadow_crossed_legs(conn, target_date=target_date.isoformat())
        for evaluation, decision in evaluated:
            if evaluation.passes and _record_shadow_entry(
                conn, ARM_FIVE_MINUTE, evaluation, decision, scan_id=run_id
            ):
                shadow_counts[ARM_FIVE_MINUTE] += 1
        for evaluation, decision in evaluated:
            recorded = evaluation.decision
            reason = evaluation.reason
            if evaluation.passes:
                if evaluation.leg in entered:
                    recorded, reason = DECISION_ALREADY_ENTERED, "leg already entered once"
                elif evaluation.leg in shadow_crossed:
                    recorded = DECISION_ALREADY_CROSSED
                    reason = "first crossing already recorded in shadow mode"
                elif halts:
                    recorded = DECISION_HALTED
                    reason = "halted by stop rule(s): " + ", ".join(
                        sorted({halt["rule"] for halt in halts})
                    )
                elif place:
                    recorded, reason = DECISION_PENDING, None
                else:
                    recorded = DECISION_SHADOW
                    reason = "M1 placement disabled (shadow mode)"
            cursor = conn.execute(
                _EVALUATION_INSERT,
                _evaluation_row(
                    evaluation,
                    decision,
                    scan_id=run_id,
                    placement_enabled=place,
                    recorded_decision=recorded,
                    reason=reason,
                ),
            )
            if cursor.rowcount != 1:
                continue  # an earlier scan this UTC hour already sampled the leg
            result.sampled += 1
            if (
                evaluation.passes
                and evaluation.agree
                and _record_shadow_entry(
                    conn, ARM_MODEL_VETO, evaluation, decision, scan_id=run_id
                )
            ):
                shadow_counts[ARM_MODEL_VETO] += 1
            if recorded == DECISION_PENDING:
                candidates.append((int(cursor.lastrowid), evaluation, decision))
        conn.commit()
    result.shadow_entries = shadow_counts
    result.candidates = len(candidates)
    for index, (evaluation_id, evaluation, decision) in enumerate(candidates):
        try:
            order_id, block_reason = admit_m1_entry(
                store,
                evaluation,
                decision,
                evaluation_id=evaluation_id,
                scan_id=run_id,
                now=current,
            )
        except Exception as exc:
            # No PENDING row may outlive this scan: the replay would read it as
            # the leg's entry and fail the next sampled crossing's real entry.
            _mark_admission_errors(
                store, [row[0] for row in candidates[index:]], exc
            )
            raise
        if order_id is not None:
            result.order_ids.append(order_id)
        else:
            result.blocked.append((evaluation.inputs.market_ticker, str(block_reason)))
    return result


def _mark_admission_errors(
    store: PaperStore, evaluation_ids: Sequence[int], exc: BaseException
) -> None:
    """Record the failed candidate and every later one as admission errors.

    Runs in a fresh transaction. If even that fails (the database is still
    locked), the rows stay PENDING, which the replay already treats as an
    account-level outcome.
    """

    reason = f"admission error: {type(exc).__name__}: {exc}"[:500]
    try:
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "UPDATE m1_evaluations SET decision=?, reason=? WHERE id=? AND decision=?",
                [
                    (DECISION_ADMISSION_ERROR, reason, int(evaluation_id), DECISION_PENDING)
                    for evaluation_id in evaluation_ids
                ],
            )
            conn.commit()
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------
# Atomic admission
# --------------------------------------------------------------------------


def _reentry_fingerprint(leg: tuple[str, str, str]) -> str:
    payload = json.dumps(
        {"account_id": M1_ACCOUNT_ID, "leg": list(leg), "policy": M1_POLICY.policy_version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _account_block_reason(
    conn: sqlite3.Connection,
    evaluation: M1LegEvaluation,
    *,
    stake: float,
    now: datetime,
) -> tuple[str | None, str | None]:
    """Return (decision, reason) when an account-level limit blocks the entry."""

    account = conn.execute(
        "SELECT status, initial_capital, opening_cash FROM paper_accounts "
        "WHERE account_id=?",
        (M1_ACCOUNT_ID,),
    ).fetchone()
    if account is None or str(account[0]) != "ACTIVE":
        return DECISION_ACCOUNT_BLOCKED, "M1 paper account is not active"
    if not (
        math.isclose(float(account[1]), M1_POLICY.reference_equity, abs_tol=1e-9)
        and math.isclose(float(account[2]), M1_POLICY.reference_equity, abs_tol=1e-9)
    ):
        return DECISION_ACCOUNT_BLOCKED, "M1 account capital does not match the policy"
    halts = m1_halts(conn)
    if halts:
        return DECISION_HALTED, "halted by stop rule(s): " + ", ".join(
            sorted({halt["rule"] for halt in halts})
        )
    realized = _realized_pnl(conn)
    if realized <= M1_POLICY.cumulative_loss_stop + _EPS:
        record_m1_halt(
            conn,
            STOP_LOSS,
            reason=f"cumulative realized P&L ${realized:.2f} reached ${M1_POLICY.cumulative_loss_stop:.2f}",
            metrics={"realized_pnl": round(realized, 2)},
            now=now,
        )
        return DECISION_HALTED, "halted by stop rule(s): loss_stop"
    entries, trading_days = _entry_counts(conn)
    target = evaluation.inputs.target_date.isoformat()
    if entries >= M1_POLICY.final_read_entries or (
        len(trading_days) >= M1_POLICY.final_read_trading_days
        and target not in trading_days
    ):
        record_m1_halt(
            conn,
            STOP_FINAL_READ,
            reason=(
                f"final read reached: {entries} entries, {len(trading_days)} trading days"
            ),
            metrics={"entries": entries, "trading_days": len(trading_days)},
            now=now,
        )
        return DECISION_HALTED, "halted by stop rule(s): final_read"
    leg = evaluation.leg
    if leg in _entered_legs(conn):
        return DECISION_ALREADY_ENTERED, "leg already entered once"
    objective_day = now.astimezone(RESEARCH_OBJECTIVE_TZ).date()
    daily = _daily_realized_pnl(conn, objective_day)
    if daily <= M1_POLICY.daily_loss_limit_dollars + _EPS:
        return DECISION_ACCOUNT_BLOCKED, (
            f"daily-loss pause: realized ${daily:.2f} reached "
            f"${M1_POLICY.daily_loss_limit_dollars:.2f}"
        )
    open_rows = conn.execute(
        f"SELECT target_date, market_ticker, UPPER(COALESCE(side, 'YES')), "
        f"contracts * cost_per_contract FROM paper_orders WHERE account_id=? "
        f"AND status IN ({','.join('?' for _ in _OPEN_STATUSES)}) "
        "AND settled_at IS NULL AND closed_at IS NULL",
        (M1_ACCOUNT_ID, *_OPEN_STATUSES),
    ).fetchall()
    series = evaluation.inputs.series_ticker
    position = sum(
        float(row[3] or 0.0)
        for row in open_rows
        if (str(row[0]), str(row[1]), str(row[2])) == leg
    )
    event = sum(
        float(row[3] or 0.0)
        for row in open_rows
        if str(row[0]) == target and str(row[1]).upper().startswith(series + "-")
    )
    target_total = sum(float(row[3] or 0.0) for row in open_rows if str(row[0]) == target)
    for label, used, cap in (
        ("position", position, M1_POLICY.position_cap_dollars),
        ("event", event, M1_POLICY.event_cap_dollars),
        ("target-date", target_total, M1_POLICY.target_date_cap_dollars),
    ):
        if used + stake > cap + _EPS:
            return DECISION_ACCOUNT_BLOCKED, (
                f"{label} exposure ${used + stake:.2f} would exceed ${cap:.2f}"
            )
    cash = float(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM paper_account_ledger WHERE account_id=?",
            (M1_ACCOUNT_ID,),
        ).fetchone()[0]
        or 0.0
    )
    if stake > cash + _EPS:
        return DECISION_ACCOUNT_BLOCKED, (
            f"account capacity: stake ${stake:.2f} exceeds available cash ${cash:.2f}"
        )
    return None, None


def admit_m1_entry(
    store: PaperStore,
    evaluation: M1LegEvaluation,
    decision: TradeDecision,
    *,
    evaluation_id: int,
    scan_id: str,
    now: datetime,
) -> tuple[int | None, str | None]:
    """Book one taker fill at the displayed ask, or record why it was blocked.

    Everything happens inside one ``BEGIN IMMEDIATE`` transaction: the leg rule
    is re-evaluated from its inputs, every account-level limit is re-read, and
    the order, its ledger debit and the evaluation's outcome commit together.
    """

    if not evaluation.passes:
        raise ValueError("M1 admission requires a leg that passes the rule")
    inputs = evaluation.inputs
    replayed = evaluate_leg(inputs)
    if not replayed.passes or replayed.contracts != evaluation.contracts:
        raise ValueError("M1 admission inputs no longer pass the frozen rule")
    ask = float(inputs.ask)
    contracts = int(evaluation.contracts)
    fee_total = order_fee(ask, contracts, series_ticker=inputs.series_ticker)
    stake = contracts * ask + fee_total
    if stake > M1_POLICY.stake_dollars + _EPS or contracts > M1_POLICY.max_contracts:
        raise ValueError("M1 entry exceeds the frozen stake or contract cap")
    fee_per_contract = fee_total / contracts
    cost_per_contract = ask + fee_per_contract
    p_trade = float(evaluation.p_trade)
    edge = p_trade - cost_per_contract
    created_at = _iso(now)
    objective_day = now.astimezone(RESEARCH_OBJECTIVE_TZ).date().isoformat()
    leg = evaluation.leg
    diagnostics = {
        "kind": "m1_market_recalibration",
        "evaluation_id": evaluation_id,
        "scan_id": scan_id,
        "policy_version": M1_POLICY.policy_version,
        "policy_fingerprint": M1_POLICY.policy_fingerprint,
        "coefficient_set_id": M1_POLICY.coefficient_set_id,
        "bucket": evaluation.bucket,
        "local_hour": evaluation.local_hour,
        "utc_hour": evaluation.utc_hour,
        "lead_days": evaluation.lead_days,
        "q_yes": evaluation.q_yes,
        "p_yes": evaluation.p_yes,
        "p_trade": p_trade,
        "gate_fee": evaluation.gate_fee,
        "gate_cost": evaluation.cost,
        "gate_edge": evaluation.edge,
        "order_fee": fee_total,
        "ask": ask,
        "bid": inputs.bid,
        "ask_size": inputs.ask_size,
        "bid_size": inputs.bid_size,
        "spread_cents": evaluation.spread_cents,
        "model_probability": inputs.model_probability,
        "agree": evaluation.agree,
        "real_money": False,
    }
    reasons = [
        (
            f"M1 market recalibration: edge {evaluation.edge:.4f} >= "
            f"{M1_POLICY.edge_threshold:.2f} ({evaluation.bucket})"
        ),
        (
            "model agrees (p_model >= cost)"
            if evaluation.agree
            else "model disagrees (p_model < cost); recorded, not gated"
        ),
    ]
    quote_snapshot = {
        "side": inputs.side,
        "bid": inputs.bid,
        "ask": ask,
        "limit_price": None,
        "contracts": float(contracts),
        "fee_total": fee_total,
        "fee_per_contract": fee_per_contract,
        "cost_per_contract": cost_per_contract,
    }
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        blocked_decision, block_reason = _account_block_reason(
            conn, evaluation, stake=stake, now=now
        )
        if blocked_decision is not None:
            updated = conn.execute(
                "UPDATE m1_evaluations SET decision=?, reason=? "
                "WHERE id=? AND decision=?",
                (blocked_decision, block_reason, evaluation_id, DECISION_PENDING),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise ValueError("M1 pending evaluation changed during admission")
            conn.commit()
            return None, block_reason
        try:
            cursor = conn.execute(
                """
                INSERT INTO paper_orders (
                    created_at, target_date, market_ticker, label, action,
                    risk_profile, side, contracts, yes_ask, entry_price,
                    entry_bid, entry_bid_size, entry_ask_size, strike_type,
                    floor_strike, cap_strike, entry_mode, fee_per_contract,
                    cost_per_contract, probability, probability_lcb, edge,
                    edge_lcb, trade_quality_score, expected_profit, status,
                    reasons_json, diagnostics_json, account_id,
                    strategy_fingerprint, sleeve, filled_at, reserved_cost,
                    quote_snapshot_json, fill_model, requested_contracts,
                    filled_contracts, remaining_contracts, queue_remaining,
                    execution_model_version, research_sleeve,
                    research_policy_version, policy_fingerprint, objective_day,
                    lead_bucket, scan_run_id, reentry_fingerprint
                ) VALUES (
                    ?, ?, ?, ?, ?, 'research', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'market', ?, ?, ?, ?, ?, ?, ?, ?, 'PAPER_FILLED', ?, ?, ?,
                    ?, 'research', ?, 0, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    created_at,
                    leg[0],
                    inputs.market_ticker,
                    decision.label,
                    f"BUY_{inputs.side}",
                    inputs.side,
                    float(contracts),
                    decision.yes_ask,
                    ask,
                    inputs.bid,
                    inputs.bid_size,
                    inputs.ask_size,
                    decision.strike_type,
                    decision.floor_strike,
                    decision.cap_strike,
                    fee_per_contract,
                    cost_per_contract,
                    p_trade,
                    p_trade,
                    edge,
                    edge,
                    decision.trade_quality_score,
                    edge * contracts,
                    json.dumps(reasons),
                    json.dumps(diagnostics, sort_keys=True),
                    M1_ACCOUNT_ID,
                    M1_POLICY.policy_fingerprint,
                    created_at,
                    json.dumps(quote_snapshot, sort_keys=True),
                    M1_FILL_MODEL,
                    float(contracts),
                    float(contracts),
                    EXECUTION_MODEL_VERSION,
                    M1_POLICY.sleeve,
                    M1_POLICY.policy_version,
                    M1_POLICY.policy_fingerprint,
                    objective_day,
                    M1_LEAD_BUCKET,
                    scan_id,
                    _reentry_fingerprint(leg),
                ),
            )
        except sqlite3.IntegrityError:
            # The one-entry-per-leg index or the account-scoped open-position
            # guard refused a second order; the leg stays entered once.
            conn.execute(
                "UPDATE m1_evaluations SET decision=?, reason=? WHERE id=? AND decision=?",
                (
                    DECISION_ALREADY_ENTERED,
                    "order uniqueness rejected a second entry",
                    evaluation_id,
                    DECISION_PENDING,
                ),
            )
            conn.commit()
            return None, "order uniqueness rejected a second entry"
        order_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO strategy_versions "
            "(fingerprint, created_at, config_json, status) VALUES (?, ?, ?, 'PAPER')",
            (
                M1_POLICY.policy_fingerprint,
                created_at,
                json.dumps(M1_POLICY.identity_payload(), sort_keys=True),
            ),
        )
        ledger = conn.execute(
            "INSERT INTO paper_account_ledger "
            "(created_at, account_id, order_id, event_type, amount, "
            "idempotency_key, details_json) VALUES (?, ?, ?, 'ENTRY_FILL', ?, ?, ?)",
            (
                created_at,
                M1_ACCOUNT_ID,
                order_id,
                -stake,
                f"order:{order_id}:entry-fill",
                json.dumps(
                    {
                        "research_sleeve": M1_POLICY.sleeve,
                        "research_policy_version": M1_POLICY.policy_version,
                        "policy_fingerprint": M1_POLICY.policy_fingerprint,
                        "filled_quantity": float(contracts),
                        "fill_model": M1_FILL_MODEL,
                        "order_fee": fee_total,
                    },
                    sort_keys=True,
                ),
            ),
        )
        if ledger.rowcount != 1:
            conn.rollback()
            raise sqlite3.IntegrityError("M1 entry ledger debit was not inserted")
        updated = conn.execute(
            "UPDATE m1_evaluations SET decision=?, reason=NULL, order_id=? "
            "WHERE id=? AND decision=?",
            (DECISION_ENTERED, order_id, evaluation_id, DECISION_PENDING),
        )
        if updated.rowcount != 1:
            conn.rollback()
            raise ValueError("M1 pending evaluation changed during admission")
        conn.commit()
    return order_id, None


# --------------------------------------------------------------------------
# Settlement truth for recorded legs
# --------------------------------------------------------------------------


def record_m1_settlement_truth(
    store: PaperStore,
    truth_by_series: Mapping[str, Mapping[str, float]],
    *,
    truth_source: str = "cli_settlement_final",
    now: datetime | None = None,
) -> int:
    """Record final CLI highs for (series, target date) pairs M1 evaluated."""

    recorded_at = _iso(now or datetime.now(UTC))
    written = 0
    with store.connect() as conn:
        needed = conn.execute(
            "SELECT DISTINCT series_ticker, target_date FROM m1_evaluations "
            "UNION SELECT DISTINCT series_ticker, target_date FROM m1_shadow_entries"
        ).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        for series, target in needed:
            high = (truth_by_series.get(str(series)) or {}).get(str(target))
            if high is None:
                continue
            high = _float_or_none(high)
            if high is None:
                continue
            existing = conn.execute(
                "SELECT settlement_high_f FROM m1_settlement_truth "
                "WHERE series_ticker=? AND target_date=?",
                (series, target),
            ).fetchone()
            if existing is not None and math.isclose(float(existing[0]), high, abs_tol=1e-9):
                continue
            conn.execute(
                "INSERT INTO m1_settlement_truth "
                "(series_ticker, target_date, settlement_high_f, truth_source, recorded_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(series_ticker, target_date) DO UPDATE SET "
                "settlement_high_f=excluded.settlement_high_f, "
                "truth_source=excluded.truth_source, recorded_at=excluded.recorded_at",
                (series, target, high, truth_source, recorded_at),
            )
            written += 1
        conn.commit()
    return written


def m1_series_dates_missing_truth(
    store: PaperStore, *, through: date, since: date | None = None
) -> dict[str, list[str]]:
    lower = since or (through - timedelta(days=RECONCILIATION_LOOKBACK_DAYS))
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT e.series_ticker, e.target_date FROM (
                SELECT series_ticker, target_date FROM m1_evaluations
                UNION SELECT series_ticker, target_date FROM m1_shadow_entries
            ) AS e
            LEFT JOIN m1_settlement_truth t
              ON t.series_ticker = e.series_ticker AND t.target_date = e.target_date
            WHERE t.series_ticker IS NULL AND e.target_date <= ? AND e.target_date >= ?
            """,
            (through.isoformat(), lower.isoformat()),
        ).fetchall()
    missing: dict[str, list[str]] = {}
    for series, target in rows:
        missing.setdefault(str(series), []).append(str(target))
    return missing


# --------------------------------------------------------------------------
# Daily reconciliation
# --------------------------------------------------------------------------


def m1_overdue_reconciliation_day(conn: sqlite3.Connection, *, now: datetime) -> str | None:
    # A pre-midnight run can suppress the :10 tick; allow 72 minutes after UTC
    # day-end for the :40 tick, 30s random delay, 1m timer accuracy and the
    # 30m service timeout (71m30s rounded up).
    cutoff_day = (now.astimezone(UTC) - timedelta(minutes=72)).date()
    cutoff, _ = _day_bounds(cutoff_day)
    row = conn.execute(
        "SELECT substr(e.created_at, 1, 10) FROM m1_evaluations e "
        "WHERE e.created_at < ? AND NOT EXISTS ("
        "SELECT 1 FROM m1_reconciliations r WHERE r.utc_day = substr(e.created_at, 1, 10)) "
        "ORDER BY e.created_at LIMIT 1",
        (cutoff,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def m1_health_problems(conn: sqlite3.Connection, *, now: datetime) -> list[str]:
    """Stale evaluations and repeated failed admissions, for host and public status.

    A failed admission or an evaluation exception leaves the parent scan green
    and still reconciles cleanly, so neither the scan stamp nor the fidelity rule
    can show it. M1 evaluates only the next civil day's ladder, so it writes
    nothing from about 07:00 to 15:00 UTC. Until 16:30 staleness is measured from
    07:00, so a trial that died before the gap stays failing through it.
    """

    now = now.astimezone(UTC)
    account = conn.execute(
        "SELECT created_at FROM paper_accounts WHERE account_id=? AND status='ACTIVE'",
        (M1_ACCOUNT_ID,),
    ).fetchone()
    newest, failures = None, []
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m1_evaluations'"
    ).fetchone():
        newest = conn.execute(
            "SELECT created_at FROM m1_evaluations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        failures = conn.execute(
            "SELECT utc_hour, created_at, decision FROM m1_evaluations "
            "WHERE created_at >= ? AND decision IN (?, ?)",
            (_iso(now - timedelta(hours=3)), DECISION_ADMISSION_ERROR, DECISION_PENDING),
        ).fetchall()

    problems = []
    failed_hours = set()
    for utc_hour, created_at, decision in failures:
        moment = parse_timestamp(created_at)
        age = now - moment if moment is not None else None
        if age is None or age > timedelta(hours=3):
            continue
        if decision == DECISION_ADMISSION_ERROR or age > timedelta(hours=1):
            failed_hours.add(utc_hour)
    if len(failed_hours) >= 2:
        problems.append(
            f"M1 trial admissions failed in {len(failed_hours)} UTC hours of the last 3"
        )
    minute_of_day = now.hour * 60 + now.minute
    in_gap = 7 * 60 <= minute_of_day < 16 * 60 + 30
    reference = now.replace(hour=7, minute=0, second=0, microsecond=0) if in_gap else now
    last = parse_timestamp(newest[0]) if newest else None
    if account is not None and last is not None and reference - last > timedelta(minutes=90):
        problems.append(
            f"M1 trial has no evaluation in the last 90 minutes (newest {newest[0]})"
        )
    if newest is None and account is not None and not in_gap:
        created = parse_timestamp(account[0])
        if created is None or now - created > timedelta(hours=24):
            problems.append(
                "M1 trial has no evaluation since account creation "
                "(24-hour start-up grace expired or unavailable)"
            )
    return problems


def _day_bounds(day: date) -> tuple[str, str]:
    lower = datetime.combine(day, time.min, tzinfo=UTC)
    return _iso(lower), _iso(lower + timedelta(days=1))


def _recorded_evaluation(row: sqlite3.Row) -> RecordedEvaluation:
    created_at = parse_timestamp(row["created_at"])
    if created_at is None:
        raise ValueError(f"M1 evaluation {row['id']} has no valid timestamp")
    return RecordedEvaluation(
        evaluation_id=int(row["id"]),
        inputs=M1LegInputs(
            created_at=created_at,
            target_date=date.fromisoformat(str(row["target_date"])),
            market_ticker=str(row["market_ticker"]),
            side=str(row["side"]),
            civil_tz=str(row["civil_tz"]),
            market_probability=row["market_probability"],
            model_probability=row["model_probability"],
            ask=row["ask"],
            bid=row["bid"],
            ask_size=row["ask_size"],
            bid_size=row["bid_size"],
            market_status=row["market_status"],
            market_close_time=row["market_close_time"],
            forecast_age_hours=row["forecast_age_hours"],
            source_spread_f=row["source_spread_f"],
            entry_gate_reason=row["entry_gate_reason"],
        ),
        placement_enabled=bool(row["placement_enabled"]),
        recorded_decision=str(row["decision"]),
        recorded_reason=row["reason"],
        recorded_ask=row["ask"],
    )


def build_m1_reconciliation(store: PaperStore, utc_day: date) -> dict[str, Any]:
    """Replay one UTC day's recorded evaluations and compare with paper entries."""

    lower, upper = _day_bounds(utc_day)
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM m1_evaluations WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at, id",
            (lower, upper),
        ).fetchall()
        prior_legs = _entered_legs(conn, before=lower)
        prior_crossed = prior_legs | _shadow_crossed_legs(conn, before=lower)
        orders = conn.execute(
            "SELECT id, created_at, target_date, market_ticker, "
            "UPPER(COALESCE(side, 'YES')) AS side, entry_price, entry_bid "
            "FROM paper_orders WHERE account_id=? AND parent_order_id IS NULL "
            "AND status != 'REJECTED' AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at, id",
            (M1_ACCOUNT_ID, lower, upper),
        ).fetchall()
        pending = [
            int(row["id"]) for row in rows if str(row["decision"]) == DECISION_PENDING
        ]
    evaluations = [_recorded_evaluation(row) for row in rows]
    expected, contradictions = replay_expected_entries(
        evaluations, already_crossed=prior_crossed
    )
    actual: list[ActualEntry] = []
    prior_reentries: list[dict[str, object]] = []
    for row in orders:
        created_at = parse_timestamp(row["created_at"])
        if created_at is None:
            raise ValueError(f"M1 order {row['id']} has no valid timestamp")
        leg = (str(row["target_date"]), str(row["market_ticker"]), str(row["side"]))
        city = city_for_market_ticker(leg[1])
        actual.append(
            ActualEntry(
                order_id=int(row["id"]),
                leg=leg,
                created_at=created_at,
                entry_price=float(row["entry_price"]),
                entry_bid=_float_or_none(row["entry_bid"]),
                civil_tz=city.civil_tz_name if city is not None else None,
            )
        )
        if leg in prior_legs:
            prior_reentries.append(
                {
                    "order_id": int(row["id"]),
                    "leg": list(leg),
                    "breach": "re-entry of a leg entered on an earlier day",
                }
            )
    matched = match_entries(expected, actual)
    breaches = [*matched["breaches"], *prior_reentries]
    return {
        "utc_day": utc_day.isoformat(),
        "policy_fingerprint": M1_POLICY.policy_fingerprint,
        "evaluations": len(evaluations),
        "expected_entries": len(expected),
        "entries": len(actual),
        "matched": matched["matched"],
        "failed": matched["failed"],
        "missed": matched["missed"],
        "breaches": breaches,
        "contradictions": contradictions,
        "pending_admissions": pending,
    }


def record_m1_reconciliation(
    store: PaperStore,
    report: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    with store.connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO m1_reconciliations (utc_day, created_at, "
            "policy_fingerprint, evaluations, entries, matched, failed, missed, "
            "breaches, contradictions, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report["utc_day"],
                _iso(now or datetime.now(UTC)),
                M1_POLICY.policy_fingerprint,
                int(report["evaluations"]),
                int(report["entries"]),
                len(report["matched"]),
                len(report["failed"]),
                len(report["missed"]),
                len(report["breaches"]),
                len(report["contradictions"]),
                json.dumps(report, sort_keys=True, default=str),
            ),
        )
        return cursor.rowcount == 1


def apply_fidelity_rule(
    store: PaperStore, utc_day: date, *, now: datetime | None = None
) -> dict[str, Any]:
    """Halt on 2+ failed matches in a rolling week, or any immediate breach."""

    window_start = (utc_day - timedelta(days=M1_POLICY.fidelity_window_days - 1)).isoformat()
    with store.connect() as conn:
        failed, breaches = conn.execute(
            "SELECT COALESCE(SUM(failed), 0), COALESCE(SUM(breaches), 0) "
            "FROM m1_reconciliations WHERE utc_day >= ? AND utc_day <= ?",
            (window_start, utc_day.isoformat()),
        ).fetchone()
        failed, breaches = int(failed or 0), int(breaches or 0)
        triggered = (
            failed >= M1_POLICY.fidelity_weekly_failures or breaches > 0
        )
        recorded = False
        if triggered:
            conn.execute("BEGIN IMMEDIATE")
            recorded = record_m1_halt(
                conn,
                STOP_FIDELITY,
                reason=(
                    f"{failed} failed replay match(es) and {breaches} breach(es) "
                    f"in the {M1_POLICY.fidelity_window_days} days to {utc_day.isoformat()}"
                ),
                metrics={
                    "window_start": window_start,
                    "window_end": utc_day.isoformat(),
                    "failed_matches": failed,
                    "breaches": breaches,
                },
                now=now,
            )
            conn.commit()
    return {
        "window_start": window_start,
        "window_end": utc_day.isoformat(),
        "failed_matches": failed,
        "breaches": breaches,
        "triggered": triggered,
        "halt_recorded": recorded,
    }


def reconcile_m1_day(
    store: PaperStore,
    utc_day: date,
    *,
    now: datetime | None = None,
    include_premise: bool = True,
) -> dict[str, Any]:
    """Build, record (once) and apply the fidelity rule for one UTC day.

    The premise refit runs with the reconciliation, once per maintenance run,
    and its result is stored in that day's report.
    """

    report = build_m1_reconciliation(store, utc_day)
    report["premise"] = check_m1_premise(store, now=now) if include_premise else None
    report["recorded"] = record_m1_reconciliation(store, report, now=now)
    report["fidelity"] = apply_fidelity_rule(store, utc_day, now=now)
    return report


# --------------------------------------------------------------------------
# Stop rules
# --------------------------------------------------------------------------


def check_m1_premise(store: PaperStore, *, now: datetime | None = None) -> dict[str, Any]:
    """Refit the pooled day-ahead slope on the trailing 30 settled target dates."""

    window = M1_POLICY.premise_window_dates
    with store.connect() as conn:
        dates = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT e.target_date FROM m1_evaluations e "
                "JOIN m1_settlement_truth t ON t.series_ticker = e.series_ticker "
                "AND t.target_date = e.target_date ORDER BY e.target_date DESC LIMIT ?",
                (window,),
            ).fetchall()
        ]
        if len(dates) < window:
            return {
                "status": "insufficient_data",
                "settled_target_dates": len(dates),
                "required_target_dates": window,
                "slope": None,
                "threshold": M1_POLICY.premise_min_slope,
            }
        rows = conn.execute(
            f"""
            SELECT e.target_date, e.market_ticker, e.created_at, e.side, e.q_yes,
                   e.bid, e.ask, e.spread_cents, e.cost, e.strike_type,
                   e.floor_strike, e.cap_strike, t.settlement_high_f
            FROM m1_evaluations e
            JOIN m1_settlement_truth t ON t.series_ticker = e.series_ticker
             AND t.target_date = e.target_date
            WHERE e.target_date IN ({','.join('?' for _ in dates)})
              AND e.lead_days = 1 AND e.listing_hour = 0 AND e.q_yes IS NOT NULL
            """,
            dates,
        ).fetchall()
    premise_rows: list[PremiseRow] = []
    for row in rows:
        outcome = resolves_yes(row[9], row[10], row[11], float(row[12]))
        if outcome is None:
            continue
        bid, ask, cents, cost = row[5], row[6], row[7], row[8]
        tradeable = (
            bid is not None
            and ask is not None
            and cents is not None
            and cost is not None
            and float(bid) >= M1_POLICY.min_entry_bid - _EPS
            and 0.0 < float(ask) <= M1_POLICY.max_entry_ask + _EPS
            and int(cents) <= M1_POLICY.max_spread_cents
            and float(cost) < 1.0
        )
        premise_rows.append(
            PremiseRow(
                target_date=str(row[0]),
                market_ticker=str(row[1]),
                scan_key=str(row[2]),
                side=str(row[3]),
                q_yes=float(row[4]),
                resolves_yes=bool(outcome),
                tradeable=tradeable,
            )
        )
    fit = premise_slope(premise_rows)
    slope = fit["slope"]
    triggered = slope is not None and float(slope) < M1_POLICY.premise_min_slope
    recorded = False
    if triggered:
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            recorded = record_m1_halt(
                conn,
                STOP_PREMISE,
                reason=(
                    f"pooled day-ahead slope {float(slope):.3f} below "
                    f"{M1_POLICY.premise_min_slope:.2f} on the trailing {window} "
                    "settled target dates"
                ),
                metrics={**fit, "first_date": min(dates), "last_date": max(dates)},
                now=now,
            )
            conn.commit()
    return {
        "status": "triggered" if triggered else ("ok" if slope is not None else "fit_failed"),
        "settled_target_dates": len(dates),
        "required_target_dates": window,
        "first_date": min(dates),
        "last_date": max(dates),
        "slope": slope,
        "intercept": fit["intercept"],
        "rows": fit["rows"],
        "threshold": M1_POLICY.premise_min_slope,
        "halt_recorded": recorded,
    }


def evaluate_m1_stop_rules(
    store: PaperStore, *, now: datetime | None = None
) -> dict[str, Any]:
    """Apply the loss, futility and final-read rules; record any new halt."""

    current = _utc_now(store, now)
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settled = _settled_entries(conn)
        realized = _realized_pnl(conn)
        entries, trading_days = _entry_counts(conn)
        settled_roi = roi(settled)
        new_halts: list[str] = []
        if realized <= M1_POLICY.cumulative_loss_stop + _EPS and record_m1_halt(
            conn,
            STOP_LOSS,
            reason=(
                f"cumulative realized P&L ${realized:.2f} reached "
                f"${M1_POLICY.cumulative_loss_stop:.2f}"
            ),
            metrics={"realized_pnl": round(realized, 2)},
            now=current,
        ):
            new_halts.append(STOP_LOSS)
        if (
            len(settled) >= M1_POLICY.futility_min_entries
            and settled_roi is not None
            and settled_roi <= M1_POLICY.futility_max_roi + _EPS
            and record_m1_halt(
                conn,
                STOP_FUTILITY,
                reason=(
                    f"date-clustered ROI {settled_roi:.2%} at or below "
                    f"{M1_POLICY.futility_max_roi:.0%} after {len(settled)} settled entries"
                ),
                metrics={"settled_entries": len(settled), "roi": settled_roi},
                now=current,
            )
        ):
            new_halts.append(STOP_FUTILITY)
        # The 60th trading day may finish its own entries. Its last day-ahead
        # hour is the end of D-1 at the westernmost station, so the horizon
        # closes at midnight starting D60 in Los Angeles, not at the UTC date.
        horizon_days_done = len(trading_days) >= M1_POLICY.final_read_trading_days and (
            current
            >= datetime.combine(
                date.fromisoformat(max(trading_days)), time.min, tzinfo=_LATEST_STATION_TZ
            )
        )
        if (
            entries >= M1_POLICY.final_read_entries or horizon_days_done
        ) and record_m1_halt(
            conn,
            STOP_FINAL_READ,
            reason=f"final read reached: {entries} entries, {len(trading_days)} trading days",
            metrics={"entries": entries, "trading_days": len(trading_days)},
            now=current,
        ):
            new_halts.append(STOP_FINAL_READ)
        conn.commit()
    return {
        "realized_pnl": realized,
        "settled_entries": len(settled),
        "settled_roi": settled_roi,
        "entries": entries,
        "trading_days": len(trading_days),
        "new_halts": new_halts,
    }


def run_m1_settle_maintenance(
    store: PaperStore,
    *,
    load_truth: Callable[[CityConfig], Mapping[date, float]] | None = None,
    cities: Iterable[CityConfig] = CITIES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Daily M1 upkeep from the existing settle timer (no new systemd timer).

    Records final CLI highs for legs M1 evaluated, reconciles complete UTC
    days not yet reconciled (oldest first, a bounded batch per run, so a
    backlog drains instead of ageing out), and applies the stop rules. Every
    step is idempotent.
    """

    current = _utc_now(store, now)
    # The ledger alone proves these stops, so a failed truth read below must not
    # delay them. The last call picks up what truth and reconciliation add.
    early_halts = evaluate_m1_stop_rules(store, now=current)["new_halts"]
    summary: dict[str, Any] = {"truth_recorded": 0, "reconciled_days": [], "stop_rules": None}
    if load_truth is not None:
        missing = m1_series_dates_missing_truth(
            store, through=current.date() - timedelta(days=1)
        )
        truth_by_series: dict[str, dict[str, float]] = {}
        for city in cities:
            if city.series_ticker not in missing:
                continue
            highs = load_truth(city) or {}
            truth_by_series[city.series_ticker] = {
                (key.isoformat() if isinstance(key, date) else str(key)): float(value)
                for key, value in highs.items()
            }
        if truth_by_series:
            summary["truth_recorded"] = record_m1_settlement_truth(
                store, truth_by_series, now=current
            )
    yesterday = current.date() - timedelta(days=1)
    with store.connect() as conn:
        first = conn.execute("SELECT MIN(created_at) FROM m1_evaluations").fetchone()[0]
        done = {
            str(row[0])
            for row in conn.execute("SELECT utc_day FROM m1_reconciliations").fetchall()
        }
    first_at = parse_timestamp(first) if first else None
    if first_at is not None:
        pending_days = []
        day = first_at.date()
        while day <= yesterday:
            if day.isoformat() not in done:
                pending_days.append(day)
            day += timedelta(days=1)
        pending_days = pending_days[:RECONCILIATION_LOOKBACK_DAYS]
        for index, day in enumerate(pending_days):
            report = reconcile_m1_day(
                store,
                day,
                now=current,
                include_premise=index == len(pending_days) - 1,
            )
            summary["reconciled_days"].append(
                {
                    "utc_day": day.isoformat(),
                    "failed": len(report["failed"]),
                    "breaches": len(report["breaches"]),
                    "missed": len(report["missed"]),
                }
            )
    stop_rules = evaluate_m1_stop_rules(store, now=current)
    summary["stop_rules"] = {
        **stop_rules,
        "new_halts": early_halts + stop_rules["new_halts"],
    }
    return summary


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------


def _shadow_arm_summary(conn: sqlite3.Connection, arm: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT s.target_date, s.side, s.strike_type, s.floor_strike, s.cap_strike,
               s.contracts, s.stake, s.agree, t.settlement_high_f
        FROM m1_shadow_entries s
        LEFT JOIN m1_settlement_truth t
          ON t.series_ticker = s.series_ticker AND t.target_date = s.target_date
        WHERE s.arm = ?
        """,
        (arm,),
    ).fetchall()
    settled: list[SettledEntry] = []
    for row in rows:
        if row[8] is None:
            continue
        outcome = resolves_yes(row[2], row[3], row[4], float(row[8]))
        if outcome is None:
            continue
        won = outcome if str(row[1]).upper() == "YES" else not outcome
        settled.append(
            SettledEntry(
                target_date=str(row[0]),
                pnl=float(row[5]) * (1.0 if won else 0.0) - float(row[6]),
                stake=float(row[6]),
                agree=None if row[7] is None else bool(row[7]),
            )
        )
    point = roi(settled)
    return {
        "arm": arm,
        "entries": len(rows),
        "settled_entries": len(settled),
        "pnl": round(math.fsum(entry.pnl for entry in settled), 2) if settled else 0.0,
        "stake": round(math.fsum(entry.stake for entry in settled), 2) if settled else 0.0,
        "roi": None if point is None else round(point, 6),
        "paper_fills": False,
    }


def _round_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (round(value, 6) if isinstance(value, float) else value)
        for key, value in stats.items()
    }


def m1_trial_payload(
    store: PaperStore,
    *,
    now: datetime | None = None,
    resamples: int = PUBLICATION_BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """The trial's pre-registration, stop-rule status and evidence, read-only."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    with store.connect() as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "m1_evaluations" not in tables:
            return {
                "available": False,
                "reason": "M1 trial tables are not initialized",
                **_static_trial_fields(),
            }
        halts = m1_halts(conn)
        settled = _settled_entries(conn)
        realized = _realized_pnl(conn)
        entries, trading_days = _entry_counts(conn)
        health_problems = m1_health_problems(conn, now=current)
        evaluation_stats = conn.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at), "
            "COALESCE(SUM(placement_enabled), 0) FROM m1_evaluations"
        ).fetchone()
        decisions = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT decision, COUNT(*) FROM m1_evaluations GROUP BY decision"
            ).fetchall()
        }
        last_reconciliation = conn.execute(
            "SELECT utc_day, failed, missed, breaches, contradictions, report_json "
            "FROM m1_reconciliations ORDER BY utc_day DESC LIMIT 1"
        ).fetchone()
        overdue_day = m1_overdue_reconciliation_day(conn, now=current)
        shadow = {arm: _shadow_arm_summary(conn, arm) for arm in SHADOW_ARMS}
        pending_cutoff = _iso(current - timedelta(hours=1))
        stale_pending = conn.execute(
            "SELECT COUNT(*) FROM m1_evaluations WHERE decision=? AND created_at < ?",
            (DECISION_PENDING, pending_cutoff),
        ).fetchone()[0]
        last_error = conn.execute(
            "SELECT created_at, reason FROM m1_evaluations WHERE decision=? "
            "ORDER BY id DESC LIMIT 1",
            (DECISION_ADMISSION_ERROR,),
        ).fetchone()
        latest_entry = conn.execute(
            "SELECT MAX(created_at) FROM paper_orders WHERE account_id=?",
            (M1_ACCOUNT_ID,),
        ).fetchone()[0]
    agree = [entry for entry in settled if entry.agree is True]
    disagree = [entry for entry in settled if entry.agree is False]
    primary = date_clustered_bootstrap(settled, resamples=resamples)
    agree_stats = date_clustered_bootstrap(agree, resamples=resamples)
    disagree_stats = date_clustered_bootstrap(disagree, resamples=resamples)
    difference = date_clustered_difference_bootstrap(agree, disagree, resamples=resamples)
    halted_rules = {halt["rule"] for halt in halts}
    premise = None
    if last_reconciliation is not None:
        try:
            premise = json.loads(last_reconciliation[5]).get("premise")
        except (json.JSONDecodeError, AttributeError):
            premise = None
    settled_roi = roi(settled)
    stop_rules = [
        {
            "rule": STOP_FIDELITY,
            "label": "Fidelity halt",
            "status": (
                "halted" if STOP_FIDELITY in halted_rules else ("overdue" if overdue_day else "ok")
            ),
            "overdue_utc_day": overdue_day,
            "threshold": (
                f">= {M1_POLICY.fidelity_weekly_failures} failed replay matches in "
                f"{M1_POLICY.fidelity_window_days} days, or any listing-hour, wide-spread, "
                "lead or re-entry breach"
            ),
            "last_reconciled_utc_day": (
                str(last_reconciliation[0]) if last_reconciliation is not None else None
            ),
            "last_failed_matches": (
                int(last_reconciliation[1]) if last_reconciliation is not None else None
            ),
            "last_breaches": (
                int(last_reconciliation[3]) if last_reconciliation is not None else None
            ),
        },
        {
            "rule": STOP_LOSS,
            "label": "Cumulative loss stop",
            "status": "halted" if STOP_LOSS in halted_rules else "ok",
            "threshold_dollars": M1_POLICY.cumulative_loss_stop,
            "realized_pnl": round(realized, 2),
        },
        {
            "rule": STOP_FUTILITY,
            "label": "Futility",
            "status": (
                "halted"
                if STOP_FUTILITY in halted_rules
                else ("ok" if len(settled) >= M1_POLICY.futility_min_entries else "not_yet_eligible")
            ),
            "min_settled_entries": M1_POLICY.futility_min_entries,
            "threshold_roi": M1_POLICY.futility_max_roi,
            "settled_entries": len(settled),
            "roi": None if settled_roi is None else round(settled_roi, 6),
        },
        {
            "rule": STOP_PREMISE,
            "label": "Premise (recalibration slope)",
            "status": (
                "paused"
                if STOP_PREMISE in halted_rules
                else (premise or {}).get("status", "not_yet_checked")
            ),
            "threshold_slope": M1_POLICY.premise_min_slope,
            "window_target_dates": M1_POLICY.premise_window_dates,
            "latest_check": premise,
        },
        {
            "rule": STOP_FINAL_READ,
            "label": "Final read",
            "status": "stopped" if STOP_FINAL_READ in halted_rules else "running",
            "entries": entries,
            "entries_limit": M1_POLICY.final_read_entries,
            "trading_days": len(trading_days),
            "trading_days_limit": M1_POLICY.final_read_trading_days,
        },
    ]
    entries_open = not halts
    return {
        "available": True,
        **_static_trial_fields(),
        "generated_at": current.isoformat(),
        "entries_enabled_by_stop_rules": entries_open,
        "halts": halts,
        "stop_rules": stop_rules,
        "evaluations": {
            "sampled_evaluations": int(evaluation_stats[0] or 0),
            "first_evaluation_at": evaluation_stats[1],
            "last_evaluation_at": evaluation_stats[2],
            "evaluations_with_placement_enabled": int(evaluation_stats[3] or 0),
            "by_decision": decisions,
        },
        "book": {
            "entries": entries,
            "trading_days": len(trading_days),
            "settled_entries": len(settled),
            "realized_pnl": round(realized, 2),
            "settled_stake": round(math.fsum(entry.stake for entry in settled), 2),
            "last_entry_at": latest_entry,
        },
        "hypotheses": [
            {
                "id": "H1",
                "role": "primary",
                "statement": "M1 ROI > 0",
                "alpha": ALPHA_PRIMARY,
                **_round_stats(primary),
                "settled_entries": len(settled),
            },
            {
                "id": "H2a",
                "role": "co-primary",
                "statement": "agree-subset ROI > 0 (p_model >= cost at entry)",
                "alpha": ALPHA_AGREE,
                **_round_stats(agree_stats),
                "settled_entries": len(agree),
            },
            {
                "id": "H2b",
                "role": "co-primary",
                "statement": "agree-subset ROI minus disagree-subset ROI > 0",
                "alpha": ALPHA_AGREE_MINUS_DISAGREE,
                **_round_stats(difference),
                "disagree_roi": (
                    None if disagree_stats["roi"] is None else round(disagree_stats["roi"], 6)
                ),
                "disagree_settled_entries": len(disagree),
            },
        ],
        "inference_note": (
            f"Date-clustered bootstrap, {resamples} resamples for this live view; "
            "the final read uses 10,000. Expected ROI after the multiple-comparisons "
            "haircut is about 0% to +2.5% (central +1%), so this run can detect "
            "failure but cannot confirm a small edge."
        ),
        "shadow_arms": shadow,
        # Derived from the journal. A scan-level exception is not stored; it
        # shows up as a stale last_evaluation_at.
        "health": {
            "problems": health_problems,
            "last_evaluation_at": evaluation_stats[2],
            "stale_pending_admissions": int(stale_pending),
            "last_error": (
                None
                if last_error is None
                else {"at": last_error[0], "reason": last_error[1]}
            ),
            "missed_entries": (
                None if last_reconciliation is None else int(last_reconciliation[2])
            ),
        },
        "last_reconciliation": (
            None
            if last_reconciliation is None
            else {
                "utc_day": str(last_reconciliation[0]),
                "failed_matches": int(last_reconciliation[1]),
                "missed_entries": int(last_reconciliation[2]),
                "breaches": int(last_reconciliation[3]),
                "contradictions": int(last_reconciliation[4]),
            }
        ),
    }


def _static_trial_fields() -> dict[str, Any]:
    return {
        "kind": "market_recalibration_trial",
        "label": "M1 market-recalibration trial (paper only)",
        "paper_only": True,
        "real_money_eligible": False,
        "counts_toward_live_readiness": False,
        "promotion_note": (
            "Nothing is promoted to real money from this run alone, and it cannot "
            "satisfy the 30-date readiness rule."
        ),
        "account_id": M1_ACCOUNT_ID,
        "profile_key": M1_PUBLISHED_PROFILE,
        "policy_version": M1_POLICY.policy_version,
        "policy_fingerprint": M1_POLICY.policy_fingerprint,
        "coefficient_set_id": M1_POLICY.coefficient_set_id,
        "coefficients": [
            {"bucket": name, "a": a, "c": c} for name, a, c in M1_POLICY.coefficients
        ],
        "preregistration": "docs/research/m1_preregistration.md",
        "initial_capital": M1_POLICY.reference_equity,
        "caps": dict(policy_summary()),
        "rule": {
            "sampling": "first research-profile scan of each UTC hour, per leg",
            "edge_threshold": M1_POLICY.edge_threshold,
            "lead_days": M1_POLICY.lead_days,
            "execution": "taker at the displayed ask; per-order market fee",
            "exits": "hold to settlement",
            "one_entry_per_leg": True,
        },
        "horizon": {
            "entries": M1_POLICY.final_read_entries,
            "trading_days": M1_POLICY.final_read_trading_days,
        },
        "expected_roi": {"low": 0.0, "central": 0.01, "high": 0.025},
    }
