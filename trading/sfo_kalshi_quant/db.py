from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import StrategyConfig, normalize_risk_profile_name
from .account import (
    AGGREGATE_RISK_PCT,
    CITY_TARGET_PCT,
    DAILY_LOSS_PCT,
    INITIAL_CAPITAL,
    MAIN_SLEEVE_PCT,
    MIN_EXECUTABLE_NOTIONAL,
    NORMAL_POSITION_CAP,
    NORMAL_POSITION_PCT,
    REGION_BY_SERIES,
    REGION_DAY_PCT,
    RESEARCH_POSITION_PCT,
    RESEARCH_SLEEVE_PCT,
    SHARED_ACCOUNT_ID,
    sleeve_for,
    strategy_fingerprint,
)
from .consensus import MarketConsensus
from .fees import (
    contracts_for_budget,
    quadratic_fee_average_per_contract,
    quadratic_fee_per_contract,
)
from .models import BucketProbability, EventSnapshot, ForecastSnapshot, IntradaySnapshot, TradeDecision
from .prediction_features import build_prediction_feature_snapshot
from .settlement_truth import normalize_settlement_truth, settlement_for_market

logger = logging.getLogger(__name__)


def _integer_settlement_high_f(value: object) -> float:
    """Round a raw daily high to the integer °F Kalshi/CLISFO settle on.

    Mirrors ``forecast._integer_settlement_high_f`` (kept here to avoid a
    circular import). Every settlement and signal-scoring path must resolve
    bins against this integer, not a fractional NWS/provisional high, so the
    paper ledger, win-rate, Brier, and calibration all agree with the
    backtest/rescore path. ``floor(x + 0.5)`` is round-half-up and idempotent
    on values that are already integers (e.g. official CLISFO highs).
    """

    high = float(value)
    if not math.isfinite(high):
        raise ValueError("settlement high must be finite")
    return float(math.floor(high + 0.5))


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_key TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    predicted_high_f REAL NOT NULL,
    fetched_at TEXT,
    method TEXT,
    source_spread_f REAL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    target_date TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probability_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    probability REAL NOT NULL,
    lower_confidence REAL NOT NULL,
    empirical_probability REAL NOT NULL,
    normal_probability REAL NOT NULL,
    effective_n REAL NOT NULL,
    residual_probability REAL,
    ensemble_probability REAL,
	    model_probability REAL,
	    market_probability REAL,
	    observed_high_f REAL,
	    intraday_probability REAL,
	    remaining_heat_risk REAL
	);

CREATE TABLE IF NOT EXISTS decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT NOT NULL,
    approved INTEGER NOT NULL,
    signal_approved INTEGER,
    entry_block_reason TEXT,
    probability REAL NOT NULL,
    probability_lcb REAL NOT NULL,
    model_probability REAL,
    market_probability REAL,
    residual_probability REAL,
    ensemble_probability REAL,
    intraday_probability REAL,
    remaining_heat_risk REAL,
    yes_bid REAL NOT NULL,
    yes_ask REAL NOT NULL,
    entry_bid REAL,
    entry_ask REAL,
    entry_bid_size REAL,
    entry_ask_size REAL,
    spread REAL NOT NULL,
    fee_per_contract REAL NOT NULL,
    cost_per_contract REAL NOT NULL,
    edge REAL NOT NULL,
    edge_lcb REAL NOT NULL,
    kelly_fraction REAL NOT NULL,
    recommended_contracts REAL NOT NULL,
    recommended_spend REAL NOT NULL,
    expected_profit REAL NOT NULL,
    trade_quality_score REAL NOT NULL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    event_ticker TEXT,
    market_status TEXT,
    market_close_time TEXT,
    forecast_fetched_at TEXT,
    forecast_method TEXT,
    forecast_observed_high_mode TEXT,
    intraday_observed_high_f REAL,
    intraday_latest_observed_at TEXT,
    intraday_is_complete INTEGER NOT NULL DEFAULT 0,
    intraday_observed_high_source TEXT,
    forecast_snapshot_id INTEGER,
    market_snapshot_id INTEGER,
    prediction_features_json TEXT,
    diagnostics_json TEXT,
    reasons_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL,
    risk_profile TEXT,
    group_id TEXT,
    side TEXT NOT NULL DEFAULT 'YES',
    contracts REAL NOT NULL,
    yes_ask REAL NOT NULL,
    entry_price REAL,
    entry_bid REAL,
    entry_bid_size REAL,
    entry_ask_size REAL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    entry_mode TEXT NOT NULL DEFAULT 'market',
    limit_price REAL,
    limit_fee_per_contract REAL,
    limit_cost_per_contract REAL,
    limit_edge REAL,
    limit_edge_lcb REAL,
    fee_per_contract REAL NOT NULL,
    cost_per_contract REAL NOT NULL,
    probability REAL NOT NULL,
    probability_lcb REAL NOT NULL,
    edge REAL NOT NULL,
    edge_lcb REAL NOT NULL,
    trade_quality_score REAL NOT NULL DEFAULT 0,
    expected_profit REAL NOT NULL,
    status TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    settled_at TEXT,
    settlement_high_f REAL,
    resolved_yes INTEGER,
    realized_pnl REAL,
    closed_at TEXT,
    exit_price REAL,
    exit_fee_per_contract REAL,
    entry_decision_snapshot_id INTEGER,
    diagnostics_json TEXT,
    outcome_diagnostics_json TEXT
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    opening_cash REAL NOT NULL,
    high_water_equity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    cutover_note TEXT
);

CREATE TABLE IF NOT EXISTS paper_account_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    account_id TEXT NOT NULL,
    order_id INTEGER,
    event_type TEXT NOT NULL,
    amount REAL NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    fingerprint TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PAPER'
);

CREATE TABLE IF NOT EXISTS paper_monitor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    market_status TEXT,
    live_bid REAL,
    exit_fee_per_contract REAL,
    net_exit_per_contract REAL,
    unrealized_pnl REAL,
    unrealized_roi REAL,
    diagnostics_json TEXT
);

CREATE TABLE IF NOT EXISTS research_shadow_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL,
    risk_profile TEXT,
    side TEXT NOT NULL DEFAULT 'YES',
    contracts REAL NOT NULL,
    yes_ask REAL NOT NULL,
    entry_price REAL,
    entry_bid REAL,
    entry_bid_size REAL,
    entry_ask_size REAL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    fee_per_contract REAL NOT NULL,
    cost_per_contract REAL NOT NULL,
    probability REAL NOT NULL,
    probability_lcb REAL NOT NULL,
    edge REAL NOT NULL,
    edge_lcb REAL NOT NULL,
    trade_quality_score REAL NOT NULL DEFAULT 0,
    expected_profit REAL NOT NULL,
    sample_probability REAL NOT NULL,
    sampled INTEGER NOT NULL DEFAULT 0,
    linked_paper_order_id INTEGER,
    status TEXT NOT NULL DEFAULT 'SHADOW_OPEN',
    reasons_json TEXT NOT NULL,
    settled_at TEXT,
    settlement_high_f REAL,
    resolved_yes INTEGER,
    realized_pnl REAL,
    closed_at TEXT,
    exit_price REAL,
    exit_fee_per_contract REAL,
    entry_decision_snapshot_id INTEGER,
    diagnostics_json TEXT,
    outcome_diagnostics_json TEXT
);

CREATE TABLE IF NOT EXISTS research_shadow_monitor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    shadow_order_id INTEGER NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    market_status TEXT,
    live_bid REAL,
    exit_fee_per_contract REAL,
    net_exit_per_contract REAL,
    unrealized_pnl REAL,
    unrealized_roi REAL,
    diagnostics_json TEXT
);
"""

# Created after column migrations in init() so they can reference late-added
# columns (e.g. group_id) on databases that predate them.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_paper_orders_market_side
    ON paper_orders (target_date, market_ticker, side, status);
CREATE INDEX IF NOT EXISTS idx_paper_orders_lifecycle
    ON paper_orders (status, settled_at, closed_at);
CREATE INDEX IF NOT EXISTS idx_paper_orders_group
    ON paper_orders (group_id);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_market
    ON decision_snapshots (target_date, market_ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_probability_snapshots_market
    ON probability_snapshots (target_date, market_ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_monitor_snapshots_order
    ON paper_monitor_snapshots (order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_research_shadow_orders_target
    ON research_shadow_orders (target_date, market_ticker, side, created_at);
CREATE INDEX IF NOT EXISTS idx_research_shadow_orders_link
    ON research_shadow_orders (linked_paper_order_id);
CREATE INDEX IF NOT EXISTS idx_research_shadow_monitor_order
    ON research_shadow_monitor_snapshots (shadow_order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_account_ledger_account
    ON paper_account_ledger (account_id, created_at, id);
"""

# Fresh databases can build this covering report index cheaply during normal
# initialization. Existing journals deliberately skip it: production creates it
# once with deploy/aws/create_decision_snapshot_index.sh while scanners are
# paused, avoiding a surprise multi-GB index build at ordinary service start.
DECISION_SNAPSHOT_REPORT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_created_market
    ON decision_snapshots (created_at, market_ticker, approved)
"""
DECISION_SNAPSHOT_SAMPLE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_pre_entry
    ON decision_snapshots (
        target_date, market_ticker, side, approved DESC, created_at, id
    )
    WHERE COALESCE(intraday_is_complete, 0) = 0
      AND market_close_time IS NOT NULL
      AND created_at < market_close_time
"""

# DB-level backstop for the application's concurrent-open guard
# (has_active_paper_entry). The app guard is a check-then-insert across separate
# connections, so a transient profile-normalization gap during a deploy (the
# 2026-06-18 duplicate-open incident) or a check-then-insert race can still leave
# two OPEN orders on the same market/side/profile. This partial UNIQUE index makes
# that physically impossible. It is SIDE-INCLUSIVE on purpose: a deliberate
# arbitrage YES+NO box on one market (opposite sides) stays legal, while a second
# OPEN order on the *identical* market/side/profile is rejected. Partial on the
# open lifecycle so re-entry after a close/settlement is unaffected. Created
# best-effort in init() because a book that still holds legacy duplicates cannot
# build it until they are closed.
OPEN_POSITION_GUARD_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_orders_open_market_side_profile
    ON paper_orders (
        target_date,
        market_ticker,
        UPPER(COALESCE(side, 'YES')),
        COALESCE(risk_profile, 'live')
    )
    WHERE status IN ('PAPER_FILLED', 'PAPER_LIMIT_RESTING')
      AND settled_at IS NULL
      AND closed_at IS NULL
"""

PAPER_ORDER_AUDIT_COLUMNS = {
    "settled_at": "TEXT",
    "settlement_high_f": "REAL",
    "resolved_yes": "INTEGER",
    "realized_pnl": "REAL",
    "closed_at": "TEXT",
    "exit_price": "REAL",
    "exit_fee_per_contract": "REAL",
    "trade_quality_score": "REAL NOT NULL DEFAULT 0",
    "side": "TEXT NOT NULL DEFAULT 'YES'",
    "entry_price": "REAL",
    "entry_bid": "REAL",
    "entry_bid_size": "REAL",
    "entry_ask_size": "REAL",
    "strike_type": "TEXT",
    "floor_strike": "REAL",
    "cap_strike": "REAL",
    "entry_mode": "TEXT NOT NULL DEFAULT 'market'",
    "limit_price": "REAL",
    "limit_fee_per_contract": "REAL",
    "limit_cost_per_contract": "REAL",
    "limit_edge": "REAL",
    "limit_edge_lcb": "REAL",
    "risk_profile": "TEXT",
    "group_id": "TEXT",
    "entry_decision_snapshot_id": "INTEGER",
    "diagnostics_json": "TEXT",
    "outcome_diagnostics_json": "TEXT",
    "account_id": "TEXT",
    "strategy_fingerprint": "TEXT",
    "sleeve": "TEXT",
    "filled_at": "TEXT",
    "cancelled_at": "TEXT",
    "expires_at": "TEXT",
    "reserved_cost": "REAL NOT NULL DEFAULT 0",
    "quote_snapshot_json": "TEXT",
    "fill_model": "TEXT",
    "fill_evidence_json": "TEXT",
}

# Fixed-PST settlement clock (UTC-8 year round) used for the daily-loss window so
# the breaker measures loss on the same day math the rest of trading settles on.
SETTLEMENT_TZ = timezone(timedelta(hours=-8))

# Rolling window (days) for the resolved-ROI circuit breaker, so a bad early
# cohort ages out and the pause can clear instead of latching off forever.
PAUSE_LOOKBACK_DAYS = 21

# Per-profile entry circuit breaker: (min_resolved_trades, max_resolved_roi,
# daily_loss_pct of bankroll). Keyed by the NORMALIZED profile name, so a key
# missing here silently disables the breaker -- both surviving profiles must be
# present. `research` keeps the tighter, earlier-tripping breaker of the two
# former collectors (fast-feedback's), since it is the tiny, loosest-gated book;
# `live` keeps the trading-intent breaker (the old balanced thresholds).
PAUSE_THRESHOLDS = {
    "live": (10, -0.35, 0.010),
    "research": (5, -0.25, 0.005),
}

PROBABILITY_AUDIT_COLUMNS = {
    "residual_probability": "REAL",
    "ensemble_probability": "REAL",
    "model_probability": "REAL",
    "market_probability": "REAL",
    "observed_high_f": "REAL",
    "intraday_probability": "REAL",
    "remaining_heat_risk": "REAL",
}

DECISION_AUDIT_COLUMNS = {
    "model_probability": "REAL",
    "market_probability": "REAL",
    "residual_probability": "REAL",
    "ensemble_probability": "REAL",
    "intraday_probability": "REAL",
    "remaining_heat_risk": "REAL",
    "event_ticker": "TEXT",
    "market_status": "TEXT",
    "market_close_time": "TEXT",
    "forecast_fetched_at": "TEXT",
    "forecast_method": "TEXT",
    "forecast_observed_high_mode": "TEXT",
    "intraday_observed_high_f": "REAL",
    "intraday_latest_observed_at": "TEXT",
    "intraday_is_complete": "INTEGER NOT NULL DEFAULT 0",
    "intraday_observed_high_source": "TEXT",
    "forecast_predicted_high_f": "REAL",
    "forecast_source_spread_f": "REAL",
    "forecast_lead_hours": "REAL",
    "risk_profile": "TEXT",
    "bankroll": "REAL",
    "forecast_snapshot_id": "INTEGER",
    "market_snapshot_id": "INTEGER",
    "prediction_features_json": "TEXT",
    "diagnostics_json": "TEXT",
    "signal_approved": "INTEGER",
    "entry_block_reason": "TEXT",
}

RESEARCH_SHADOW_AUDIT_COLUMNS = {
    "entry_decision_snapshot_id": "INTEGER",
    "diagnostics_json": "TEXT",
    "outcome_diagnostics_json": "TEXT",
}

MONITOR_AUDIT_COLUMNS = {
    "diagnostics_json": "TEXT",
}


class PaperStore:
    def __init__(self, db_path: Path, *, init: bool = True) -> None:
        self.db_path = Path(db_path)
        if init:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.init()

    def connect(self) -> sqlite3.Connection:
        # Five+ systemd units (scan, monitor, settle, strategy-lab, forecaster)
        # touch this database concurrently. WAL plus a real busy_timeout lets
        # readers and a single writer coexist instead of failing fast with
        # "database is locked" on the default 5s rollback-journal connection.
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            # Non-file databases (e.g. :memory:) ignore WAL; never block init.
            pass
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            decision_table_existed = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='decision_snapshots'"
            ).fetchone() is not None
            conn.executescript(SCHEMA)
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()
            }
            for column, column_type in PAPER_ORDER_AUDIT_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE paper_orders ADD COLUMN {column} {column_type}")
            existing_probability = {
                row[1]
                for row in conn.execute("PRAGMA table_info(probability_snapshots)").fetchall()
            }
            for column, column_type in PROBABILITY_AUDIT_COLUMNS.items():
                if column not in existing_probability:
                    conn.execute(f"ALTER TABLE probability_snapshots ADD COLUMN {column} {column_type}")
            existing_decision = {
                row[1]
                for row in conn.execute("PRAGMA table_info(decision_snapshots)").fetchall()
            }
            for column, column_type in DECISION_AUDIT_COLUMNS.items():
                if column not in existing_decision:
                    conn.execute(f"ALTER TABLE decision_snapshots ADD COLUMN {column} {column_type}")
            existing_shadow = {
                row[1]
                for row in conn.execute("PRAGMA table_info(research_shadow_orders)").fetchall()
            }
            for column, column_type in RESEARCH_SHADOW_AUDIT_COLUMNS.items():
                if column not in existing_shadow:
                    conn.execute(f"ALTER TABLE research_shadow_orders ADD COLUMN {column} {column_type}")
            for table in ("paper_monitor_snapshots", "research_shadow_monitor_snapshots"):
                existing_monitor = {
                    row[1]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, column_type in MONITOR_AUDIT_COLUMNS.items():
                    if column not in existing_monitor:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            existing_forecast = {
                row[1]
                for row in conn.execute("PRAGMA table_info(forecast_snapshots)").fetchall()
            }
            if "station_id" not in existing_forecast:
                conn.execute(
                    "ALTER TABLE forecast_snapshots ADD COLUMN station_id TEXT DEFAULT 'KSFO'"
                )
            _migrate_legacy_profile_names(conn)
            conn.executescript(INDEXES)
            if not decision_table_existed:
                conn.execute(DECISION_SNAPSHOT_REPORT_INDEX)
                conn.execute(DECISION_SNAPSHOT_SAMPLE_INDEX)
            elif (
                conn.execute(
                    "SELECT 1 FROM decision_snapshots LIMIT 1"
                ).fetchone()
                and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                    ("idx_decision_snapshots_created_market",),
                ).fetchone()
                is None
            ):
                logger.warning(
                    "decision_snapshots is nonempty but "
                    "idx_decision_snapshots_created_market is missing; pause paper "
                    "scan/monitor and run deploy/aws/create_decision_snapshot_index.sh"
                )
            self._ensure_open_position_guard_index(conn)
            self._ensure_shared_paper_account(conn)

    def _ensure_shared_paper_account(self, conn: sqlite3.Connection) -> None:
        if conn.execute(
            "SELECT 1 FROM paper_accounts WHERE account_id = ?", (SHARED_ACCOUNT_ID,)
        ).fetchone():
            return
        active = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE status IN "
            "('PAPER_FILLED', 'PAPER_LIMIT_RESTING') AND settled_at IS NULL AND closed_at IS NULL"
        ).fetchone()
        if active and int(active[0] or 0) > 0:
            # Cutover must happen flat.  Existing monitoring/settlement remains
            # available, but account_policy_capacity() will block new entries.
            return
        realized = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_orders "
            "WHERE status IN ('PAPER_SETTLED', 'PAPER_CLOSED')"
        ).fetchone()
        opening_cash = INITIAL_CAPITAL + float(realized[0] or 0.0)
        created_at = _now()
        conn.execute(
            "INSERT INTO paper_accounts "
            "(account_id, created_at, initial_capital, opening_cash, high_water_equity, cutover_note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                SHARED_ACCOUNT_ID,
                created_at,
                INITIAL_CAPITAL,
                opening_cash,
                opening_cash,
                "flat-book shared-account v2 cutover",
            ),
        )
        self._record_ledger_event(
            conn,
            account_id=SHARED_ACCOUNT_ID,
            order_id=None,
            event_type="OPENING_CASH",
            amount=opening_cash,
            idempotency_key=f"{SHARED_ACCOUNT_ID}:opening",
            details={"initial_capital": INITIAL_CAPITAL, "legacy_realized_pnl": opening_cash - INITIAL_CAPITAL},
        )

    @staticmethod
    def _record_ledger_event(
        conn: sqlite3.Connection,
        *,
        account_id: str,
        order_id: int | None,
        event_type: str,
        amount: float,
        idempotency_key: str,
        details: dict[str, object] | None = None,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO paper_account_ledger "
            "(created_at, account_id, order_id, event_type, amount, idempotency_key, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _now(), account_id, order_id, event_type, float(amount), idempotency_key,
                json.dumps(details or {}, sort_keys=True),
            ),
        )

    def shared_account_state(self) -> dict[str, object] | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_accounts'"
            ).fetchone() is None:
                return None
            account = conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (SHARED_ACCOUNT_ID,)
            ).fetchone()
            if account is None:
                return None
            cash = float(conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM paper_account_ledger WHERE account_id = ?",
                (SHARED_ACCOUNT_ID,),
            ).fetchone()[0] or 0.0)
            risk = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status='PAPER_FILLED' AND settled_at IS NULL AND closed_at IS NULL "
                "THEN contracts * cost_per_contract ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status='PAPER_LIMIT_RESTING' AND settled_at IS NULL AND closed_at IS NULL "
                "THEN reserved_cost ELSE 0 END), 0) FROM paper_orders WHERE account_id = ?",
                (SHARED_ACCOUNT_ID,),
            ).fetchone()
            open_cost = float(risk[0] or 0.0)
            reservations = float(risk[1] or 0.0)
            cash_balance = cash + reservations
            realized_equity = cash_balance + open_cost
            high_water = max(float(account["high_water_equity"]), realized_equity)
            if high_water > float(account["high_water_equity"]):
                conn.execute(
                    "UPDATE paper_accounts SET high_water_equity=? WHERE account_id=?",
                    (high_water, SHARED_ACCOUNT_ID),
                )
            return {
                "account_id": SHARED_ACCOUNT_ID,
                "initial_capital": float(account["initial_capital"]),
                "opening_cash": float(account["opening_cash"]),
                "cash_balance": cash_balance,
                "open_cost_basis": open_cost,
                "reservations": reservations,
                "available_cash": cash,
                "realized_equity": realized_equity,
                "high_water_equity": high_water,
                "drawdown": (high_water - realized_equity) / high_water if high_water > 0 else 0.0,
                "status": account["status"],
            }

    def account_policy_capacity(
        self,
        *,
        target_date: str,
        market_ticker: str,
        risk_profile: str | None,
        requested_spend: float,
    ) -> dict[str, object]:
        """Maximum safe new notional under the one-account paper policy."""

        state = self.shared_account_state()
        if state is None:
            return {"allowed_spend": 0.0, "reason": "shared account cutover requires a flat book"}
        equity = float(state["realized_equity"])
        drawdown = float(state["drawdown"])
        if drawdown >= 0.15:
            return {"allowed_spend": 0.0, "reason": "15% account drawdown pause"}
        series = market_ticker.split("-", 1)[0].upper()
        region = REGION_BY_SERIES.get(series, "unknown")
        profile = normalize_risk_profile_name(risk_profile) if risk_profile else "live"
        today_start = datetime.now(SETTLEMENT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.connect() as conn:
            daily_pnl = float(conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_orders "
                "WHERE status IN ('PAPER_SETTLED', 'PAPER_CLOSED') "
                "AND COALESCE(closed_at, settled_at) >= ?",
                (today_start.astimezone(UTC).isoformat(),),
            ).fetchone()[0] or 0.0)
            if daily_pnl <= -DAILY_LOSS_PCT * equity:
                return {"allowed_spend": 0.0, "reason": "2% shared-account daily loss pause"}
            active = conn.execute(
                "SELECT market_ticker, target_date, COALESCE(risk_profile, 'live'), "
                "CASE WHEN status='PAPER_LIMIT_RESTING' THEN reserved_cost "
                "ELSE contracts * cost_per_contract END AS risk "
                "FROM paper_orders WHERE account_id=? AND status IN "
                "('PAPER_FILLED','PAPER_LIMIT_RESTING') AND settled_at IS NULL AND closed_at IS NULL",
                (SHARED_ACCOUNT_ID,),
            ).fetchall()
        aggregate = sum(float(row[3] or 0.0) for row in active)
        research_risk = sum(float(row[3] or 0.0) for row in active if str(row[2]) == "research")
        main_risk = aggregate - research_risk
        city_risk = sum(
            float(row[3] or 0.0) for row in active
            if str(row[0]).startswith(series + "-") and str(row[1]) == target_date
        )
        region_risk = sum(
            float(row[3] or 0.0) for row in active
            if REGION_BY_SERIES.get(str(row[0]).split("-", 1)[0].upper(), "unknown") == region
            and str(row[1]) == target_date
        )
        position_cap = (
            RESEARCH_POSITION_PCT * equity
            if profile == "research"
            else min(NORMAL_POSITION_CAP, NORMAL_POSITION_PCT * equity)
        )
        if drawdown >= 0.10:
            position_cap *= 0.5
        total_room = AGGREGATE_RISK_PCT * equity - aggregate
        if profile == "research":
            sleeve_room = RESEARCH_SLEEVE_PCT * equity - research_risk
        else:
            # Main can borrow whatever portion of the 4% research sleeve is idle.
            sleeve_room = MAIN_SLEEVE_PCT * equity - main_risk + max(
                0.0, RESEARCH_SLEEVE_PCT * equity - research_risk
            )
        allowed = min(
            requested_spend,
            position_cap,
            total_room,
            sleeve_room,
            CITY_TARGET_PCT * equity - city_risk,
            REGION_DAY_PCT * equity - region_risk,
            float(state["available_cash"]),
        )
        if requested_spend < MIN_EXECUTABLE_NOTIONAL:
            return {"allowed_spend": 0.0, "reason": "recommendation below $5 executable minimum"}
        if allowed < MIN_EXECUTABLE_NOTIONAL:
            return {"allowed_spend": 0.0, "reason": "account risk room below $5 executable minimum"}
        return {"allowed_spend": max(0.0, allowed), "reason": None}

    def _ensure_open_position_guard_index(self, conn: sqlite3.Connection) -> None:
        """Build the unique open-position backstop index, tolerating a dirty book.

        A database that still holds pre-existing duplicate OPEN orders (a book
        from before this guard, e.g. the 2026-06-18 incident) cannot build the
        unique index. Rather than brick every init()/scan, log which groups block
        it so an operator can close the surplus with `paper-close`; the index then
        builds automatically on the next run.
        """
        try:
            conn.execute(OPEN_POSITION_GUARD_INDEX)
        except sqlite3.IntegrityError:
            offending = conn.execute(
                """
                SELECT market_ticker,
                       UPPER(COALESCE(side, 'YES')) AS side,
                       COALESCE(risk_profile, 'live') AS risk_profile,
                       COUNT(*) AS open_orders
                FROM paper_orders
                WHERE status IN ('PAPER_FILLED', 'PAPER_LIMIT_RESTING')
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                GROUP BY 1, 2, 3
                HAVING COUNT(*) > 1
                """
            ).fetchall()
            logger.warning(
                "open-position guard index not built: %d duplicate open group(s) "
                "must be closed first (e.g. via `paper-close`): %s",
                len(offending),
                "; ".join(f"{row[3]}x {row[0]} {row[1]} [{row[2]}]" for row in offending),
            )

    def record_forecast(self, forecast: ForecastSnapshot) -> int:
        created_at = _now()
        raw = {
            **forecast.raw,
            "lead_hours": forecast.lead_hours,
            "google_high_f": forecast.google_high_f,
            "nws_high_f": forecast.nws_high_f,
            "open_meteo_high_f": forecast.open_meteo_high_f,
            "history_high_f": forecast.history_high_f,
            "google_weight": forecast.google_weight,
            "nws_weight": forecast.nws_weight,
            "open_meteo_weight": forecast.open_meteo_weight,
            "history_weight": forecast.history_weight,
            "station_adjustment_f": forecast.station_adjustment_f,
            "fresh_station_count": forecast.fresh_station_count,
            "max_calls_per_day": forecast.max_calls_per_day,
            "calls_used_today": forecast.calls_used_today,
        }
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO forecast_snapshots (
                    created_at, target_date, station_id, predicted_high_f, fetched_at, method, source_spread_f, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    forecast.target_date.isoformat(),
                    forecast.station_id,
                    forecast.predicted_high_f,
                    forecast.fetched_at,
                    forecast.method,
                    forecast.source_spread_f,
                    json.dumps(raw, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def record_market(self, event: EventSnapshot) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO market_snapshots (created_at, event_ticker, target_date, raw_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    _now(),
                    event.event_ticker,
                    event.target_date.isoformat() if event.target_date else None,
                    json.dumps(event.raw, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def latest_market_snapshot(self, target_date: str) -> EventSnapshot | None:
        """Reconstruct the most recent stored Kalshi ladder for a target date.

        ``record_market`` persists the full Kalshi event payload (the same
        ``with_nested_markets`` body that ``EventSnapshot.from_kalshi`` parses) as
        ``raw_json``, so the freshest snapshot round-trips losslessly back into an
        ``EventSnapshot`` -- bid/ask ladder and all. The Strategy Lab builder uses
        this to distill the market consensus offline (it never touches live
        Kalshi). Returns None when no snapshot was ever stored for the target or
        when the stored payload is unparseable.
        """

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT raw_json
                FROM market_snapshots
                WHERE target_date = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (target_date,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return EventSnapshot.from_kalshi(payload)

    def record_probabilities(self, target_date: str, probabilities: Iterable[BucketProbability]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO probability_snapshots (
                    created_at, target_date, market_ticker, label, probability,
                    lower_confidence, empirical_probability, normal_probability, effective_n,
	                    residual_probability, ensemble_probability,
	                    model_probability, market_probability, observed_high_f,
	                    intraday_probability, remaining_heat_risk
	                )
	                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        _now(),
                        target_date,
                        probability.ticker,
                        probability.label,
                        probability.probability,
                        probability.lower_confidence,
                        probability.empirical_probability,
                        probability.normal_probability,
                        probability.effective_n,
                        probability.residual_probability,
                        probability.ensemble_probability,
	                        probability.model_probability,
	                        probability.market_probability,
	                        probability.observed_high_f,
	                        probability.intraday_probability,
	                        probability.remaining_heat_risk,
	                    )
                    for probability in probabilities
                ],
            )

    def latest_model_probability(
        self,
        target_date: str,
        market_ticker: str,
        *,
        max_age_minutes: float = 90.0,
    ) -> float | None:
        """Most recent pure weather-model YES probability, or None if stale.

        The paper monitor uses this to veto stop-loss exits that the model
        still expects to win at settlement; a stale snapshot must not veto.
        This intentionally reads the model_probability column (the weather
        model alone), not the market-blended posterior, so the veto reflects
        the model's own conviction rather than the book it is trying to beat.
        Older rows that predate model_probability fall back to the blend.

        Source of truth is ``decision_snapshots`` (written on EVERY scan tick).
        ``probability_snapshots`` is only written on the first command of the
        first profile per tick (the ``--skip-context-snapshots`` dedup); when a
        scan run never reaches that path the context tables flatline while the
        decision journal keeps flowing -- which silently disabled this veto from
        2026-06-16 and let the naked price-stop whipsaw NO favorites the model
        still expected to win. Read the live decision journal first (normalizing
        the per-side ``model_probability`` back to the YES frame), and fall back
        to ``probability_snapshots`` only when no fresh decision row exists.
        """

        fresh = self._latest_model_probability_from_decisions(
            target_date, market_ticker, max_age_minutes=max_age_minutes
        )
        if fresh is not None:
            return fresh
        return self._latest_model_probability_from_snapshots(
            target_date, market_ticker, max_age_minutes=max_age_minutes
        )

    def _latest_model_probability_from_decisions(
        self,
        target_date: str,
        market_ticker: str,
        *,
        max_age_minutes: float,
    ) -> float | None:
        """Latest decision-journal model probability, normalized to the YES frame.

        ``decision_snapshots`` stores ``model_probability`` per SIDE (a BUY_NO
        row carries the NO-side model probability), so flip NO rows back to YES.
        """

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, side, COALESCE(model_probability, probability)
                FROM decision_snapshots
                WHERE target_date = ? AND market_ticker = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (target_date, market_ticker),
            ).fetchone()
        if row is None or row[2] is None:
            return None
        if not self._snapshot_is_fresh(row[0], max_age_minutes):
            return None
        value = float(row[2])
        side = str(row[1]).upper()
        yes_probability = value if side == "YES" else 1.0 - value
        return max(0.0, min(1.0, yes_probability))

    def _latest_model_probability_from_snapshots(
        self,
        target_date: str,
        market_ticker: str,
        *,
        max_age_minutes: float,
    ) -> float | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, COALESCE(model_probability, probability)
                FROM probability_snapshots
                WHERE target_date = ? AND market_ticker = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (target_date, market_ticker),
            ).fetchone()
        if row is None or row[1] is None:
            return None
        if not self._snapshot_is_fresh(row[0], max_age_minutes):
            return None
        return float(row[1])

    @staticmethod
    def _snapshot_is_fresh(created_at: object, max_age_minutes: float) -> bool:
        try:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_minutes = (datetime.now(UTC) - created).total_seconds() / 60.0
        return age_minutes <= max_age_minutes

    def record_decisions(
        self,
        target_date: str,
        decisions: Iterable[TradeDecision],
        *,
        forecast: ForecastSnapshot | None = None,
        intraday: IntradaySnapshot | None = None,
        event: EventSnapshot | None = None,
        market_consensus: MarketConsensus | None = None,
        risk_profile: str | None = None,
        bankroll: float | None = None,
        strategy_config: StrategyConfig | None = None,
        forecast_snapshot_id: int | None = None,
        market_snapshot_id: int | None = None,
    ) -> None:
        created_at = _now()
        rows = []
        markets_by_ticker = {}
        if event is not None:
            markets_by_ticker = {market.ticker: market for market in event.markets}
        observed_high_mode = _forecast_observed_high_mode(forecast)
        prediction_features = build_prediction_feature_snapshot(
            forecast,
            market_consensus=market_consensus,
            intraday=intraday,
        )
        prediction_features_json = json.dumps(
            prediction_features,
            sort_keys=True,
        )
        for decision in decisions:
            spend = decision.recommended_contracts * decision.cost_per_contract
            market = markets_by_ticker.get(decision.ticker)
            diagnostics_json = json.dumps(
                _decision_diagnostics_payload(
                    target_date,
                    decision,
                    created_at=created_at,
                    forecast=forecast,
                    intraday=intraday,
                    event=event,
                    market=market,
                    market_consensus=market_consensus,
                    prediction_features=prediction_features,
                    risk_profile=risk_profile,
                    bankroll=bankroll,
                    strategy_config=strategy_config,
                    forecast_snapshot_id=forecast_snapshot_id,
                    market_snapshot_id=market_snapshot_id,
                ),
                sort_keys=True,
            )
            rows.append(
                (
                    created_at,
                    target_date,
                    decision.ticker,
                    decision.label,
                    decision.action,
                    decision.side,
                    1 if decision.approved else 0,
                    1
                    if (
                        decision.signal_approved
                        if decision.signal_approved is not None
                        else decision.approved
                    )
                    else 0,
                    decision.entry_block_reason,
                    decision.probability,
                    decision.probability_lcb,
                    decision.model_probability,
                    decision.market_probability,
                    decision.residual_probability,
                    decision.ensemble_probability,
                    decision.intraday_probability,
                    decision.remaining_heat_risk,
                    decision.yes_bid,
                    decision.yes_ask,
                    decision.bid,
                    decision.ask,
                    decision.bid_size,
                    decision.ask_size,
                    decision.spread,
                    decision.fee_per_contract,
                    decision.cost_per_contract,
                    decision.edge,
                    decision.edge_lcb,
                    decision.kelly_fraction,
                    decision.recommended_contracts,
                    spend,
                    decision.expected_profit,
                    decision.trade_quality_score,
                    decision.strike_type,
                    decision.floor_strike,
                    decision.cap_strike,
                    event.event_ticker if event is not None else None,
                    market.status if market is not None else None,
                    _market_close_time(market.raw) if market is not None else None,
                    forecast.fetched_at if forecast is not None else None,
                    forecast.method if forecast is not None else None,
                    observed_high_mode,
                    intraday.observed_high_f if intraday is not None else None,
                    intraday.latest_observed_at if intraday is not None else None,
                    1 if intraday is not None and intraday.is_complete else 0,
                    intraday.observed_high_source if intraday is not None else None,
                    forecast.predicted_high_f if forecast is not None else None,
                    forecast.source_spread_f if forecast is not None else None,
                    forecast.lead_hours if forecast is not None else None,
                    risk_profile,
                    bankroll,
                    forecast_snapshot_id,
                    market_snapshot_id,
                    prediction_features_json,
                    diagnostics_json,
                    json.dumps(decision.reasons),
                )
            )
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO decision_snapshots (
                    created_at, target_date, market_ticker, label, action, side,
                    approved, signal_approved, entry_block_reason,
                    probability, probability_lcb, model_probability,
                    market_probability, residual_probability, ensemble_probability,
                    intraday_probability, remaining_heat_risk, yes_bid, yes_ask,
                    entry_bid, entry_ask, entry_bid_size, entry_ask_size, spread,
                    fee_per_contract, cost_per_contract, edge, edge_lcb,
                    kelly_fraction, recommended_contracts, recommended_spend,
                    expected_profit, trade_quality_score, strike_type, floor_strike,
                    cap_strike, event_ticker, market_status, market_close_time,
                    forecast_fetched_at, forecast_method, forecast_observed_high_mode,
                    intraday_observed_high_f, intraday_latest_observed_at,
                    intraday_is_complete, intraday_observed_high_source,
                    forecast_predicted_high_f, forecast_source_spread_f,
                    forecast_lead_hours, risk_profile, bankroll,
                    forecast_snapshot_id, market_snapshot_id,
                    prediction_features_json, diagnostics_json, reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def record_paper_order(
        self,
        target_date: str,
        decision: TradeDecision,
        *,
        risk_profile: str | None = None,
        status: str | None = None,
        entry_mode: str = "market",
        group_id: str | None = None,
        strategy_config: StrategyConfig | None = None,
    ) -> int | None:
        contracts = float(decision.recommended_contracts)
        entry_price = float(decision.limit_price if decision.limit_price is not None else decision.ask)
        normalized_status = status or ("PAPER_FILLED" if decision.approved else "REJECTED")
        fee_per_contract = (
            float(decision.limit_fee_per_contract)
            if decision.limit_fee_per_contract is not None
            else quadratic_fee_average_per_contract(
                entry_price,
                contracts,
                maker=normalized_status == "PAPER_LIMIT_RESTING",
                series_ticker=decision.ticker,
            )
        )
        cost_per_contract = entry_price + fee_per_contract
        edge = float(decision.limit_edge) if decision.limit_edge is not None else float(decision.edge)
        edge_lcb = (
            float(decision.limit_edge_lcb)
            if decision.limit_edge_lcb is not None
            else float(decision.edge_lcb)
        )
        expected_profit = edge * contracts
        profile = normalize_risk_profile_name(risk_profile) if risk_profile else None
        created_at = _now()
        filled_at = created_at if normalized_status == "PAPER_FILLED" else None
        expires_at = (
            (datetime.fromisoformat(created_at) + timedelta(minutes=15)).isoformat()
            if normalized_status == "PAPER_LIMIT_RESTING"
            else None
        )
        reserved_cost = contracts * cost_per_contract if normalized_status == "PAPER_LIMIT_RESTING" else 0.0
        fingerprint = strategy_fingerprint(strategy_config, entry_mode=entry_mode)
        sleeve = sleeve_for(profile, list(decision.reasons), decision.side)
        quote_snapshot_json = json.dumps(
            {
                "side": decision.side,
                "bid": decision.bid,
                "ask": decision.ask,
                "limit_price": decision.limit_price,
                "contracts": contracts,
                "fee_per_contract": fee_per_contract,
                "cost_per_contract": cost_per_contract,
            },
            sort_keys=True,
        )
        fill_model = (
            "maker_trade_through_required"
            if normalized_status == "PAPER_LIMIT_RESTING"
            else "immediate_visible_quote"
        )
        with self.connect() as conn:
            entry_decision = _latest_entry_decision_snapshot(
                conn,
                target_date,
                decision,
                risk_profile=profile,
            )
            diagnostics_json = json.dumps(
                _order_entry_diagnostics_payload(
                    target_date,
                    decision,
                    created_at=created_at,
                    kind="paper_order",
                    risk_profile=profile,
                    status=normalized_status,
                    entry_mode=entry_mode,
                    group_id=group_id,
                    strategy_config=strategy_config,
                    sample_probability=None,
                    sampled=None,
                    entry_decision=entry_decision,
                ),
                sort_keys=True,
            )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO paper_orders (
                        created_at, target_date, market_ticker, label, action, risk_profile,
                        group_id,
                        side, contracts, yes_ask, entry_price, entry_bid, entry_bid_size, entry_ask_size,
                        strike_type, floor_strike, cap_strike, entry_mode,
                        limit_price, limit_fee_per_contract, limit_cost_per_contract, limit_edge, limit_edge_lcb,
                        fee_per_contract, cost_per_contract, probability,
                        probability_lcb, edge, edge_lcb, trade_quality_score,
                        expected_profit, status, entry_decision_snapshot_id,
                        diagnostics_json, reasons_json, account_id,
                        strategy_fingerprint, sleeve, filled_at, expires_at,
                        reserved_cost, quote_snapshot_json, fill_model
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        target_date,
                        decision.ticker,
                        decision.label,
                        decision.action,
                        profile,
                        group_id,
                        decision.side,
                        contracts,
                        entry_price,
                        entry_price,
                        decision.bid,
                        decision.bid_size,
                        decision.ask_size,
                        decision.strike_type,
                        decision.floor_strike,
                        decision.cap_strike,
                        entry_mode,
                        decision.limit_price,
                        decision.limit_fee_per_contract,
                        decision.limit_cost_per_contract,
                        decision.limit_edge,
                        decision.limit_edge_lcb,
                        fee_per_contract,
                        cost_per_contract,
                        decision.probability,
                        decision.probability_lcb,
                        edge,
                        edge_lcb,
                        decision.trade_quality_score,
                        expected_profit,
                        normalized_status,
                        _row_value(entry_decision, "id") if entry_decision is not None else None,
                        diagnostics_json,
                        json.dumps(decision.reasons),
                        SHARED_ACCOUNT_ID,
                        fingerprint,
                        sleeve,
                        filled_at,
                        expires_at,
                        reserved_cost,
                        quote_snapshot_json,
                        fill_model,
                    ),
                )
            except sqlite3.IntegrityError:
                # The open-position guard index rejected a second OPEN order on
                # this market/side/profile -- a check-then-insert race the
                # application guard (has_active_paper_entry) did not catch. The
                # existing open order stands; signal "not recorded" to the caller.
                return None
            order_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT OR IGNORE INTO strategy_versions "
                "(fingerprint, created_at, config_json, status) VALUES (?, ?, ?, 'PAPER')",
                (
                    fingerprint,
                    created_at,
                    json.dumps(_strategy_config_snapshot(strategy_config) or {}, sort_keys=True),
                ),
            )
            if normalized_status == "PAPER_LIMIT_RESTING":
                self._record_ledger_event(
                    conn,
                    account_id=SHARED_ACCOUNT_ID,
                    order_id=order_id,
                    event_type="RESERVE",
                    amount=-reserved_cost,
                    idempotency_key=f"order:{order_id}:reserve",
                    details={"expires_at": expires_at},
                )
            elif normalized_status == "PAPER_FILLED":
                self._record_ledger_event(
                    conn,
                    account_id=SHARED_ACCOUNT_ID,
                    order_id=order_id,
                    event_type="ENTRY_FILL",
                    amount=-(contracts * cost_per_contract),
                    idempotency_key=f"order:{order_id}:entry-fill",
                )
            return order_id

    def record_research_shadow_order(
        self,
        target_date: str,
        decision: TradeDecision,
        *,
        risk_profile: str | None,
        sample_probability: float,
        sampled: bool,
        linked_paper_order_id: int | None = None,
        strategy_config: StrategyConfig | None = None,
    ) -> int:
        contracts = float(decision.recommended_contracts)
        entry_price = float(decision.limit_price if decision.limit_price is not None else decision.ask)
        fee_per_contract = (
            float(decision.limit_fee_per_contract)
            if decision.limit_fee_per_contract is not None
            else quadratic_fee_average_per_contract(entry_price, contracts)
        )
        cost_per_contract = entry_price + fee_per_contract
        edge = float(decision.limit_edge) if decision.limit_edge is not None else float(decision.edge)
        edge_lcb = (
            float(decision.limit_edge_lcb)
            if decision.limit_edge_lcb is not None
            else float(decision.edge_lcb)
        )
        expected_profit = edge * contracts
        profile = normalize_risk_profile_name(risk_profile) if risk_profile else None
        created_at = _now()
        with self.connect() as conn:
            entry_decision = _latest_entry_decision_snapshot(
                conn,
                target_date,
                decision,
                risk_profile=profile,
            )
            diagnostics_json = json.dumps(
                _order_entry_diagnostics_payload(
                    target_date,
                    decision,
                    created_at=created_at,
                    kind="research_shadow_order",
                    risk_profile=profile,
                    status="SHADOW_OPEN",
                    entry_mode="shadow",
                    group_id=None,
                    strategy_config=strategy_config,
                    sample_probability=sample_probability,
                    sampled=sampled,
                    entry_decision=entry_decision,
                ),
                sort_keys=True,
            )
            cursor = conn.execute(
                """
                INSERT INTO research_shadow_orders (
                    created_at, target_date, market_ticker, label, action,
                    risk_profile, side, contracts, yes_ask, entry_price,
                    entry_bid, entry_bid_size, entry_ask_size, strike_type,
                    floor_strike, cap_strike, fee_per_contract,
                    cost_per_contract, probability, probability_lcb, edge,
                    edge_lcb, trade_quality_score, expected_profit,
                    sample_probability, sampled, linked_paper_order_id,
                    status, entry_decision_snapshot_id, diagnostics_json,
                    reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    target_date,
                    decision.ticker,
                    decision.label,
                    decision.action,
                    profile,
                    decision.side,
                    contracts,
                    decision.yes_ask,
                    entry_price,
                    decision.bid,
                    decision.bid_size,
                    decision.ask_size,
                    decision.strike_type,
                    decision.floor_strike,
                    decision.cap_strike,
                    fee_per_contract,
                    cost_per_contract,
                    decision.probability,
                    decision.probability_lcb,
                    edge,
                    edge_lcb,
                    decision.trade_quality_score,
                    expected_profit,
                    sample_probability,
                    1 if sampled else 0,
                    linked_paper_order_id,
                    "SHADOW_OPEN",
                    _row_value(entry_decision, "id") if entry_decision is not None else None,
                    diagnostics_json,
                    json.dumps(decision.reasons),
                ),
            )
            return int(cursor.lastrowid)

    def link_research_shadow_order(self, shadow_order_id: int, paper_order_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE research_shadow_orders
                SET linked_paper_order_id = ?, sampled = 1
                WHERE id = ?
                """,
                (paper_order_id, shadow_order_id),
            )

    def research_shadow_orders(
        self,
        limit: int = 50,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[sqlite3.Row]:
        filters, params = _date_filters(since, until)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"SELECT * FROM research_shadow_orders {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

    def research_shadow_sample_spend_for_target(
        self,
        target_date: str,
        *,
        risk_profile: str | None = None,
    ) -> float:
        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(p.contracts * p.cost_per_contract), 0)
                FROM research_shadow_orders s
                JOIN paper_orders p ON p.id = s.linked_paper_order_id
                WHERE s.target_date = ?
                  AND s.sampled = 1
                  AND p.status != 'REJECTED'
                  {profile_filter.replace('COALESCE(risk_profile,', 'COALESCE(p.risk_profile,')}
                """,
                (target_date, *profile_params),
            ).fetchone()
        return float(row[0] or 0.0)

    def has_losing_closed_negative_lcb_research_entry(
        self,
        target_date: str,
        market_ticker: str,
        side: str,
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM paper_orders
                WHERE target_date = ?
                  AND market_ticker = ?
                  AND UPPER(COALESCE(side, 'YES')) = ?
                  AND COALESCE(risk_profile, 'live') = 'research'
                  AND status = 'PAPER_CLOSED'
                  AND settled_at IS NULL
                  AND closed_at IS NOT NULL
                  AND edge_lcb < 0
                  AND COALESCE(realized_pnl, 0) < 0
                LIMIT 1
                """,
                (target_date, market_ticker, side.upper()),
            ).fetchone()
        return row is not None

    def record_manual_buy(
        self,
        *,
        target_date: str,
        market_ticker: str,
        label: str,
        amount: float,
        entry_price: float,
        side: str = "YES",
        action: str,
        reason: str,
        strike_type: str | None = None,
        floor_strike: float | None = None,
        cap_strike: float | None = None,
    ) -> int:
        side = side.upper()
        if side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        if amount <= 0:
            raise ValueError("amount must be positive")
        if entry_price <= 0 or entry_price >= 1:
            raise ValueError("entry price must be between 0.01 and 0.99")
        # Kalshi trades whole contracts. Fractional dust also breaks the
        # ceil-to-cent fee model (a 0.01-contract order would carry a $1.00
        # per-contract fee), so manual paper buys round down to whole contracts.
        contracts = float(int(contracts_for_budget(entry_price, amount)))
        if contracts < 1:
            raise ValueError(
                f"amount ${amount:.2f} cannot buy one whole contract at {entry_price:.2f} plus fees"
            )
        fee = quadratic_fee_average_per_contract(entry_price, contracts)
        cost = entry_price + fee
        decision = TradeDecision(
            ticker=market_ticker,
            label=label,
            action=action,
            approved=True,
            probability=0.0,
            probability_lcb=0.0,
            yes_bid=0.0,
            yes_ask=entry_price,
            spread=0.0,
            fee_per_contract=fee,
            cost_per_contract=cost,
            edge=0.0,
            edge_lcb=0.0,
            kelly_fraction=0.0,
            recommended_contracts=contracts,
            expected_profit=0.0,
            reasons=[reason],
            side=side,
            entry_bid=0.0,
            entry_ask=entry_price,
            strike_type=strike_type,
            floor_strike=floor_strike,
            cap_strike=cap_strike,
        )
        order_id = self.record_paper_order(target_date, decision)
        if order_id is None:
            raise ValueError(
                "an open paper position already exists for this market/side/profile"
            )
        return order_id

    def record_manual_yes_buy(
        self,
        *,
        target_date: str,
        market_ticker: str,
        label: str,
        amount: float,
        entry_price: float,
        action: str,
        reason: str,
    ) -> int:
        return self.record_manual_buy(
            target_date=target_date,
            market_ticker=market_ticker,
            label=label,
            amount=amount,
            entry_price=entry_price,
            side="YES",
            action=action,
            reason=reason,
        )

    def paper_spend_for_target(
        self,
        target_date: str,
        *,
        risk_profile: str | None = None,
        series_ticker: str | None = None,
    ) -> float:
        """Capital already deployed for one settlement target.

        ``series_ticker`` scopes the cap to one city's event (multi-city scans
        cap per city-day, not across all fifteen cities sharing one date).
        """

        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        series_filter = ""
        series_params: tuple = ()
        if series_ticker:
            series_filter = " AND market_ticker LIKE ?"
            series_params = (f"{series_ticker}-%",)
        with self.connect() as conn:
            # Exclude PAPER_EXPIRED: those are resting limit orders that never
            # crossed and deployed ZERO capital, so they must not consume the
            # per-target exposure cap. Counting their intended-but-never-filled
            # notional inflated cumulative spend and blocked valid re-entries on
            # the next scan -- exactly the cap-freeing the settle path documents
            # when it expires them. (REJECTED was never placed.)
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(contracts * cost_per_contract), 0)
                FROM paper_orders
                WHERE target_date = ? AND status NOT IN ('REJECTED', 'PAPER_EXPIRED')
                {series_filter}
                {profile_filter}
                """,
                (target_date, *series_params, *profile_params),
            ).fetchone()
        return float(row[0] or 0.0)

    def remaining_daily_budget(
        self,
        target_date: str,
        daily_budget: float,
        *,
        risk_profile: str | None = None,
    ) -> float:
        if daily_budget < 0:
            raise ValueError("daily budget cannot be negative")
        spent = self.paper_spend_for_target(target_date, risk_profile=risk_profile)
        return max(0.0, daily_budget - spent)

    def entries_for_market_side(
        self,
        target_date: str,
        market_ticker: str,
        side: str,
        *,
        risk_profile: str | None = None,
    ) -> int:
        """Count all recorded paper entries for a market/side and target date.

        Unlike has_open_paper_position, this also counts closed and settled
        orders, so a stop-loss exit cannot be followed by an immediate re-buy
        on the next scheduled scan.

        PAPER_EXPIRED is excluded alongside REJECTED: an expired resting maker
        quote never filled, deployed zero capital, and carries no position or
        realized outcome. Counting it against max_entries_per_market_side
        (live=1) permanently blocked that market-side for the whole target
        date after one unfilled 15-minute quote, so an approved edge could
        never re-quote (e.g. expired order #109 blocked 32 later approved
        snapshots). Resting quotes still count while they rest, and
        filled/closed/settled entries still count forever.
        """

        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM paper_orders
                WHERE target_date = ?
                  AND market_ticker = ?
                  AND UPPER(COALESCE(side, 'YES')) = ?
                  AND status NOT IN ('REJECTED', 'PAPER_EXPIRED')
                  {profile_filter}
                """,
                (target_date, market_ticker, side.upper(), *profile_params),
            ).fetchone()
        return int(row[0] or 0)

    def has_open_paper_position(
        self,
        target_date: str,
        market_ticker: str,
        side: str | None = None,
        *,
        risk_profile: str | None = None,
    ) -> bool:
        filters = [
            "target_date = ?",
            "market_ticker = ?",
            "status = 'PAPER_FILLED'",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker]
        if side is not None:
            filters.append("UPPER(side) = ?")
            params.append(side.upper())
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

    def has_active_paper_entry(
        self,
        target_date: str,
        market_ticker: str,
        *,
        risk_profile: str | None = None,
    ) -> bool:
        filters = [
            "target_date = ?",
            "market_ticker = ?",
            "status IN ('PAPER_FILLED', 'PAPER_LIMIT_RESTING')",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

    def resting_limit_orders(
        self,
        target_date: str,
        market_ticker: str,
        side: str,
        *,
        risk_profile: str | None = None,
    ) -> list[sqlite3.Row]:
        filters = [
            "target_date = ?",
            "market_ticker = ?",
            "UPPER(COALESCE(side, 'YES')) = ?",
            "status = 'PAPER_LIMIT_RESTING'",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker, side.upper()]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"""
                SELECT *
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                ORDER BY created_at, id
                """,
                params,
            ).fetchall()

    def fill_resting_limit_order(
        self, order_id: int, *, evidence: dict[str, object] | None = None
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE id=? AND status='PAPER_LIMIT_RESTING'",
                (order_id,),
            ).fetchone()
            if row is None:
                return self._order(order_id)
            filled_at = _now()
            cursor = conn.execute(
                """
                UPDATE paper_orders
                SET status = 'PAPER_FILLED', filled_at = ?, reserved_cost = 0,
                    fill_evidence_json = ?
                WHERE id = ? AND status = 'PAPER_LIMIT_RESTING'
                """,
                (filled_at, json.dumps(evidence or {}, sort_keys=True), order_id),
            )
            if cursor.rowcount:
                reserved = float(row["reserved_cost"] or 0.0)
                if row["account_id"]:
                    self._record_ledger_event(
                        conn, account_id=row["account_id"],
                        order_id=order_id, event_type="RESERVATION_RELEASE", amount=reserved,
                        idempotency_key=f"order:{order_id}:fill-release",
                    )
                    self._record_ledger_event(
                        conn, account_id=row["account_id"],
                        order_id=order_id, event_type="ENTRY_FILL",
                        amount=-(float(row["contracts"]) * float(row["cost_per_contract"])),
                        idempotency_key=f"order:{order_id}:entry-fill",
                        details=evidence,
                    )
        return self._order(order_id)

    def cancel_resting_limit_order(self, order_id: int, *, reason: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE id=? AND status='PAPER_LIMIT_RESTING'",
                (order_id,),
            ).fetchone()
            if row is None:
                return self._order(order_id)
            cancelled_at = _now()
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_EXPIRED', cancelled_at=?, "
                "reserved_cost=0, outcome_diagnostics_json=? WHERE id=?",
                (cancelled_at, json.dumps({"event": "cancellation", "reason": reason}, sort_keys=True), order_id),
            )
            if row["account_id"]:
                self._record_ledger_event(
                    conn, account_id=row["account_id"],
                    order_id=order_id, event_type="RESERVATION_RELEASE",
                    amount=float(row["reserved_cost"] or 0.0),
                    idempotency_key=f"order:{order_id}:cancel-release",
                    details={"reason": reason},
                )
        return self._order(order_id)

    def expire_stale_resting_orders(self, *, now: str | None = None) -> int:
        cutoff = now or _now()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM paper_orders WHERE status='PAPER_LIMIT_RESTING' "
                "AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at, id",
                (cutoff,),
            ).fetchall()
        expired = 0
        for (order_id,) in rows:
            row = self.cancel_resting_limit_order(int(order_id), reason="15-minute maker TTL expired")
            expired += int(row is not None and row["status"] == "PAPER_EXPIRED")
        return expired

    def record_monitor_snapshot(
        self,
        order: sqlite3.Row,
        *,
        side: str,
        action: str,
        reason: str | None = None,
        market_status: str | None = None,
        live_bid: float | None = None,
        exit_fee_per_contract: float | None = None,
        net_exit_per_contract: float | None = None,
        unrealized_pnl: float | None = None,
        unrealized_roi: float | None = None,
    ) -> int:
        created_at = _now()
        diagnostics_json = json.dumps(
            _monitor_diagnostics_payload(
                order,
                created_at=created_at,
                side=side,
                action=action,
                reason=reason,
                market_status=market_status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee_per_contract,
                net_exit_per_contract=net_exit_per_contract,
                unrealized_pnl=unrealized_pnl,
                unrealized_roi=unrealized_roi,
            ),
            sort_keys=True,
        )
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO paper_monitor_snapshots (
                    created_at, order_id, target_date, market_ticker, side,
                    action, reason, market_status, live_bid,
                    exit_fee_per_contract, net_exit_per_contract,
                    unrealized_pnl, unrealized_roi, diagnostics_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    int(order["id"]),
                    order["target_date"],
                    order["market_ticker"],
                    side.upper(),
                    action,
                    reason,
                    market_status,
                    live_bid,
                    exit_fee_per_contract,
                    net_exit_per_contract,
                    unrealized_pnl,
                    unrealized_roi,
                    diagnostics_json,
                ),
            )
            return int(cursor.lastrowid)

    def paper_orders(
        self,
        limit: int = 50,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[sqlite3.Row]:
        filters, params = _date_filters(since, until)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"SELECT * FROM paper_orders {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

    def settle_paper_orders(
        self,
        target_date: str,
        settlement_high_f: float,
        *,
        series_ticker: str | None = None,
    ) -> int:
        # Resolve bins against the integer °F Kalshi settles on, never a
        # fractional NWS/provisional high. Without this, a true high of 65.4
        # would settle the 65-or-below bin differently than Kalshi does, and a
        # value straddling a half-degree bin edge (e.g. 65.5 -> 66) flips the
        # YES/NO outcome. This single chokepoint covers manual --settlement-high,
        # CLISFO, and the WeatherEdge ground-truth fallback.
        settlement_high_f = _integer_settlement_high_f(settlement_high_f)
        settled_at = _now()
        settled = 0
        series_filter = ""
        series_params: tuple = ()
        if series_ticker:
            # A settlement high is one station's number; it must never resolve
            # another city's bins that happen to share the calendar date.
            series_filter = " AND market_ticker LIKE ?"
            series_params = (f"{series_ticker}-%",)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            # Reserve the writer slot BEFORE reading the unsettled rows so the
            # snapshot and the conditional UPDATEs are one atomic transaction.
            # The read used to run on its own connection (_unsettled_orders),
            # closing before this writer opened — a TOCTOU window in which the
            # monitor (every ~2 minutes) could close a position between read and
            # write. BEGIN IMMEDIATE takes SQLite's RESERVED lock now, so a
            # concurrent close either blocks on busy_timeout until we commit or
            # commits first (and is then visible in our snapshot). The per-row
            # status/closed_at guard below stays as defense-in-depth.
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE target_date = ? AND status = 'PAPER_FILLED' AND settled_at IS NULL
                """ + series_filter,
                (target_date, *series_params),
            ).fetchall()
            # Resting limit orders that never crossed before this target
            # resolved can never fill now. Expire them (zero PnL) so they stop
            # consuming the per-target exposure cap, blocking re-entry, and
            # showing as perpetual pending exposure on the dashboard.
            resting_rows = conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE target_date = ?
                  AND status = 'PAPER_LIMIT_RESTING'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                """ + series_filter,
                (target_date, *series_params),
            ).fetchall()
            for row in resting_rows:
                outcome_json = json.dumps(
                    _outcome_diagnostics_payload(
                        row,
                        event="expiration",
                        resolved_at=settled_at,
                        settlement_high_f=settlement_high_f,
                        resolved_yes=None,
                        position_won=None,
                        realized_pnl=0.0,
                    ),
                    sort_keys=True,
                )
                conn.execute(
                    """
                    UPDATE paper_orders
                    SET status = 'PAPER_EXPIRED',
                        settled_at = ?,
                        cancelled_at = ?,
                        settlement_high_f = ?,
                        realized_pnl = 0.0,
                        reserved_cost = 0,
                        outcome_diagnostics_json = ?
                    WHERE id = ?
                      AND status = 'PAPER_LIMIT_RESTING'
                      AND settled_at IS NULL
                      AND closed_at IS NULL
                    """,
                    (settled_at, settled_at, settlement_high_f, outcome_json, row["id"]),
                )
                if row["account_id"]:
                    self._record_ledger_event(
                        conn, account_id=row["account_id"],
                        order_id=int(row["id"]), event_type="RESERVATION_RELEASE",
                        amount=float(row["reserved_cost"] or 0.0),
                        idempotency_key=f"order:{row['id']}:settlement-expire-release",
                    )
            for row in rows:
                resolved_yes = _row_resolves_yes(row, settlement_high_f)
                side = _row_side(row)
                position_wins = resolved_yes if side == "YES" else not resolved_yes
                cost = float(row["cost_per_contract"])
                contracts = float(row["contracts"])
                realized_pnl = contracts * ((1.0 - cost) if position_wins else -cost)
                outcome_json = json.dumps(
                    _outcome_diagnostics_payload(
                        row,
                        event="settlement",
                        resolved_at=settled_at,
                        settlement_high_f=settlement_high_f,
                        resolved_yes=resolved_yes,
                        position_won=position_wins,
                        realized_pnl=realized_pnl,
                    ),
                    sort_keys=True,
                )
                # The status/closed_at guard makes settlement a no-op on a row a
                # concurrent monitor close already flipped, instead of silently
                # overwriting its realized_pnl. Count real row changes, not the
                # read size.
                cursor = conn.execute(
                    """
                    UPDATE paper_orders
                    SET
                        settled_at = ?,
                        settlement_high_f = ?,
                        resolved_yes = ?,
                        realized_pnl = ?,
                        status = 'PAPER_SETTLED',
                        outcome_diagnostics_json = ?
                    WHERE id = ?
                      AND status = 'PAPER_FILLED'
                      AND settled_at IS NULL
                      AND closed_at IS NULL
                    """,
                    (
                        settled_at,
                        settlement_high_f,
                        1 if resolved_yes else 0,
                        realized_pnl,
                        outcome_json,
                        row["id"],
                    ),
                )
                if cursor.rowcount:
                    proceeds = contracts if position_wins else 0.0
                    if row["account_id"]:
                        self._record_ledger_event(
                            conn, account_id=row["account_id"],
                            order_id=int(row["id"]), event_type="SETTLEMENT_PROCEEDS",
                            amount=proceeds,
                            idempotency_key=f"order:{row['id']}:settlement-proceeds",
                            details={"position_won": position_wins},
                        )
                settled += cursor.rowcount
        return settled

    def close_paper_order(self, order_id: int, exit_price: float) -> sqlite3.Row:
        if exit_price <= 0 or exit_price >= 1:
            raise ValueError("exit price must be between 0.01 and 0.99")
        row = self._open_order(order_id)
        if row is None:
            raise ValueError(f"no open paper order found with id {order_id}")
        contracts = float(row["contracts"])
        entry_cost = float(row["cost_per_contract"])
        exit_fee = quadratic_fee_average_per_contract(
            exit_price, contracts, series_ticker=str(row["market_ticker"])
        )
        realized_pnl = contracts * (exit_price - exit_fee - entry_cost)
        # Persist resolved_yes on close so a closed order is classified by the
        # same resolved_yes-driven path as a settled order (db.py settle path),
        # not the realized_pnl > 0 fallback in _row_position_won. A break-even
        # close (realized_pnl == 0) is recorded as resolved_yes = NULL so it is
        # treated as undecided rather than silently bucketed as a loss.
        side = _row_side(row)
        if abs(realized_pnl) < 1e-9:
            resolved_yes: int | None = None
        else:
            position_won = realized_pnl > 0.0
            resolved_yes = 1 if (position_won if side == "YES" else not position_won) else 0
        closed_at = _now()
        outcome_json = json.dumps(
            _outcome_diagnostics_payload(
                row,
                event="close",
                resolved_at=closed_at,
                settlement_high_f=None,
                resolved_yes=bool(resolved_yes) if resolved_yes is not None else None,
                position_won=None if abs(realized_pnl) < 1e-9 else realized_pnl > 0.0,
                realized_pnl=realized_pnl,
                exit_price=exit_price,
                exit_fee_per_contract=exit_fee,
            ),
            sort_keys=True,
        )
        with self.connect() as conn:
            # Guard the close on the same open-state predicate settle uses, then
            # require it to have actually changed a row. Between _open_order()
            # above and this UPDATE, a concurrent settle (the q2min monitor and
            # the settle path race on one DB) can flip this order to
            # PAPER_SETTLED. A bare WHERE id = ? would then overwrite the true
            # settlement outcome with an intraday exit price, permanently
            # corrupting the paper PnL ledger, equity curve, and circuit breaker.
            cursor = conn.execute(
                """
                UPDATE paper_orders
                SET
                    status = 'PAPER_CLOSED',
                    closed_at = ?,
                    exit_price = ?,
                    exit_fee_per_contract = ?,
                    resolved_yes = ?,
                    realized_pnl = ?,
                    outcome_diagnostics_json = ?
                WHERE id = ?
                  AND status = 'PAPER_FILLED'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                """,
                (
                    closed_at,
                    exit_price,
                    exit_fee,
                    resolved_yes,
                    realized_pnl,
                    outcome_json,
                    order_id,
                ),
            )
            if cursor.rowcount == 0:
                # Already settled/closed concurrently. Raise instead of returning
                # the resolved row so the caller does not double-book it; the
                # paper-monitor loop catches ValueError/RuntimeError per order and
                # keeps inspecting the rest of the book.
                raise ValueError(
                    f"paper order {order_id} was resolved concurrently before close"
                )
            net_proceeds = contracts * (exit_price - exit_fee)
            if row["account_id"]:
                self._record_ledger_event(
                    conn, account_id=row["account_id"], order_id=order_id,
                    event_type="EXIT_PROCEEDS", amount=net_proceeds,
                    idempotency_key=f"order:{order_id}:exit-proceeds",
                    details={"exit_price": exit_price, "exit_fee_per_contract": exit_fee},
                )
        closed = self._order(order_id)
        if closed is None:
            raise RuntimeError(f"paper order {order_id} disappeared after close")
        return closed

    def open_paper_order(self, order_id: int) -> sqlite3.Row | None:
        return self._open_order(order_id)

    def resting_paper_orders(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Every live resting maker limit order, for the monitor's fill pass."""

        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT *
                FROM paper_orders
                WHERE status = 'PAPER_LIMIT_RESTING'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                ORDER BY created_at, id
                """
            params: tuple[object, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                params = (limit,)
            return conn.execute(query, params).fetchall()

    def open_paper_orders(self, limit: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT *
                FROM paper_orders
                WHERE status = 'PAPER_FILLED'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                ORDER BY created_at DESC
                """
            params: tuple[object, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                params = (limit,)
            return conn.execute(query, params).fetchall()

    def open_no_basket_orders(
        self,
        target_date: str,
        *,
        risk_profile: str | None = None,
    ) -> list[sqlite3.Row]:
        filters = [
            "target_date = ?",
            "status = 'PAPER_FILLED'",
            "settled_at IS NULL",
            "closed_at IS NULL",
            "UPPER(COALESCE(side, 'YES')) = 'NO'",
        ]
        params: list[object] = [target_date]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"""
                SELECT *
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                ORDER BY created_at, id
                """,
                params,
            ).fetchall()

    def prune_decision_snapshots(
        self,
        *,
        full_days: int = 7,
        dedup_days: int = 45,
    ) -> dict[str, int]:
        """Retention for the highest-volume table on the disk-bound box.

        Fifteen cities at a 5-minute scan write ~60k rejection snapshots
        (~0.5 GB) per day; unbounded growth filled the old single-city box and
        thrashed the strategy-lab pass. Policy: everything stays full-fidelity
        for ``full_days``; between ``full_days`` and ``dedup_days`` only the
        LAST snapshot per (market, side, target_date) survives -- the
        end-of-day context of why the book said what it said -- plus every
        approved/signal-approved row; beyond ``dedup_days`` only approved
        rows remain. Approved rows are never deleted.
        """

        if full_days < 1 or dedup_days <= full_days:
            raise ValueError("need dedup_days > full_days >= 1")
        with self.connect() as conn:
            dedup_cursor = conn.execute(
                """
                DELETE FROM decision_snapshots
                WHERE created_at < datetime('now', ?)
                  AND created_at >= datetime('now', ?)
                  AND COALESCE(approved, 0) = 0
                  AND COALESCE(signal_approved, 0) = 0
                  AND id NOT IN (
                      SELECT MAX(id) FROM decision_snapshots
                      GROUP BY market_ticker, side, target_date
                  )
                """,
                (f"-{full_days} days", f"-{dedup_days} days"),
            )
            drop_cursor = conn.execute(
                """
                DELETE FROM decision_snapshots
                WHERE created_at < datetime('now', ?)
                  AND COALESCE(approved, 0) = 0
                  AND COALESCE(signal_approved, 0) = 0
                """,
                (f"-{dedup_days} days",),
            )
            return {
                "deduped": dedup_cursor.rowcount,
                "dropped": drop_cursor.rowcount,
            }

    def open_paper_target_dates(self, *, series_ticker: str | None = None) -> list[str]:
        query = """
            SELECT DISTINCT target_date
            FROM paper_orders
            WHERE status IN ('PAPER_FILLED', 'PAPER_LIMIT_RESTING')
              AND settled_at IS NULL
              AND closed_at IS NULL
        """
        params: tuple = ()
        if series_ticker:
            query += " AND market_ticker LIKE ?"
            params = (f"{series_ticker}-%",)
        query += " ORDER BY target_date"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [str(row[0]) for row in rows]

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
            if row["status"] == "PAPER_FILLED" and row["realized_pnl"] is None
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

    def paper_equity(self, starting_bankroll: float, *, risk_profile: str | None = None) -> float:
        """Live paper equity = starting bankroll + realized PnL to date.

        Kelly and the percentage risk caps should fraction CURRENT wealth, not a
        frozen notional. This is the realized-equity base used when
        size_against_live_equity is enabled (open-position mark-to-market is left
        out so the value is deterministic for a given settled history).
        """

        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(realized_pnl), 0)
                FROM paper_orders
                WHERE realized_pnl IS NOT NULL
                  AND status != 'REJECTED'
                  AND status != 'PAPER_EXPIRED'
                  {profile_filter}
                """,
                tuple(profile_params),
            ).fetchone()
        return float(starting_bankroll) + float(row[0] or 0.0)

    def paper_entry_pause_reason(
        self,
        risk_profile: str | None,
        *,
        bankroll: float,
        target_date: str,
        min_resolved_trades: int | None = None,
        max_resolved_roi: float | None = None,
        daily_loss_pct: float | None = None,
        lookback_days: int = PAUSE_LOOKBACK_DAYS,
        now: datetime | None = None,
    ) -> str | None:
        """Circuit breaker for paper entries, per profile.

        Extends the original fast-feedback-only breaker to the trading-intent
        profiles (the ones that could one day fund real money) with looser
        thresholds. The resolved-ROI gate now uses a rolling lookback window so
        an unlucky early cohort can age out and the pause can clear, and the
        daily-loss gate measures loss realized on the current fixed-PST
        settlement day (via closed_at/settled_at) rather than loss attributable
        to whichever target date happens to be settling.
        """

        profile = normalize_risk_profile_name(risk_profile)
        thresholds = PAUSE_THRESHOLDS.get(profile)
        if thresholds is None:
            return None
        d_min, d_roi, d_daily = thresholds
        min_resolved_trades = d_min if min_resolved_trades is None else min_resolved_trades
        max_resolved_roi = d_roi if max_resolved_roi is None else max_resolved_roi
        daily_loss_pct = d_daily if daily_loss_pct is None else daily_loss_pct

        now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        window_start = (now_utc - timedelta(days=lookback_days)).isoformat()
        pst_now = now_utc.astimezone(SETTLEMENT_TZ)
        day_start = (
            pst_now.replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
            .isoformat()
        )

        with self.connect() as conn:
            resolved = conn.execute(
                """
                SELECT
                    COUNT(*) AS trades,
                    COALESCE(SUM(realized_pnl), 0) AS pnl,
                    COALESCE(SUM(contracts * cost_per_contract), 0) AS capital
                FROM paper_orders
                WHERE realized_pnl IS NOT NULL
                  AND status != 'REJECTED'
                  AND status != 'PAPER_EXPIRED'
                  AND COALESCE(risk_profile, 'live') = ?
                  AND COALESCE(closed_at, settled_at) >= ?
                """,
                (profile, window_start),
            ).fetchone()
            daily = conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl), 0) AS pnl
                FROM paper_orders
                WHERE realized_pnl IS NOT NULL
                  AND status != 'REJECTED'
                  AND status != 'PAPER_EXPIRED'
                  AND COALESCE(risk_profile, 'live') = ?
                  AND COALESCE(closed_at, settled_at) >= ?
                """,
                (profile, day_start),
            ).fetchone()

        trades = int(resolved[0] or 0)
        pnl = float(resolved[1] or 0.0)
        capital = float(resolved[2] or 0.0)
        roi = pnl / capital if capital > 0 else 0.0
        if trades >= min_resolved_trades and roi <= max_resolved_roi:
            return (
                f"{profile} paused: resolved ROI {roi:.1%} across "
                f"{trades} paper trade(s) in the last {lookback_days}d is below "
                f"{max_resolved_roi:.0%}; recording near-misses only"
            )

        daily_pnl = float(daily[0] or 0.0)
        daily_loss_limit = -abs(float(bankroll) * daily_loss_pct)
        if daily_pnl <= daily_loss_limit:
            return (
                f"{profile} paused: daily loss ${daily_pnl:.2f} reached "
                f"${daily_loss_limit:.2f}; recording near-misses only"
            )
        return None

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
                                   UPPER(COALESCE(side, CASE
                                       WHEN instr(UPPER(action), 'NO') > 0 THEN 'NO'
                                       ELSE 'YES'
                                   END))
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

    def _open_order(self, order_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE id = ?
                    AND status = 'PAPER_FILLED'
                    AND settled_at IS NULL
                    AND closed_at IS NULL
                """,
                (order_id,),
            ).fetchone()

    def paper_order(self, order_id: int) -> sqlite3.Row | None:
        """Public read of one stored paper order, as booked."""

        return self._order(order_id)

    def _order(self, order_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM paper_orders WHERE id = ?", (order_id,)).fetchone()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _forecast_observed_high_mode(forecast: ForecastSnapshot | None) -> str | None:
    if forecast is None or not isinstance(forecast.raw, dict):
        return None
    decision = forecast.raw.get("observed_high_decision")
    if not isinstance(decision, dict):
        return None
    mode = decision.get("mode")
    return str(mode).lower() if mode else None


def _market_close_time(raw: dict | None) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("close_time") or raw.get("expected_expiration_time") or raw.get("expiration_time")
    return str(value) if value else None


def _decision_diagnostics_payload(
    target_date: str,
    decision: TradeDecision,
    *,
    created_at: str,
    forecast: ForecastSnapshot | None,
    intraday: IntradaySnapshot | None,
    event: EventSnapshot | None,
    market,
    market_consensus: MarketConsensus | None,
    prediction_features: dict[str, object],
    risk_profile: str | None,
    bankroll: float | None,
    strategy_config: StrategyConfig | None,
    forecast_snapshot_id: int | None,
    market_snapshot_id: int | None,
) -> dict[str, object]:
    return _drop_none(
        {
            "schema_version": 1,
            "kind": "trade_decision",
            "created_at": created_at,
            "target_date": target_date,
            "risk_profile": risk_profile,
            "bankroll": _round_number(bankroll),
            "context_refs": {
                "forecast_snapshot_id": forecast_snapshot_id,
                "market_snapshot_id": market_snapshot_id,
            },
            "signal": _decision_signal_payload(decision),
            "forecast": _forecast_diagnostics_payload(forecast),
            "intraday": _intraday_diagnostics_payload(intraday),
            "market": _market_diagnostics_payload(market, event),
            "market_consensus": _market_consensus_diagnostics_payload(market_consensus),
            "prediction_features": dict(prediction_features or {}),
            "strategy_config": _strategy_config_snapshot(strategy_config),
        }
    )


def _order_entry_diagnostics_payload(
    target_date: str,
    decision: TradeDecision,
    *,
    created_at: str,
    kind: str,
    risk_profile: str | None,
    status: str,
    entry_mode: str,
    group_id: str | None,
    strategy_config: StrategyConfig | None,
    sample_probability: float | None,
    sampled: bool | None,
    entry_decision: sqlite3.Row | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": kind,
        "created_at": created_at,
        "target_date": target_date,
        "risk_profile": risk_profile,
        "status": status,
        "entry_mode": entry_mode,
        "group_id": group_id,
        "signal": _decision_signal_payload(decision),
        "strategy_config": _strategy_config_snapshot(strategy_config),
        "entry_decision": _entry_decision_ref_payload(entry_decision),
    }
    if sample_probability is not None or sampled is not None:
        payload["sampling"] = {
            "sample_probability": _round_number(sample_probability),
            "sampled": sampled,
        }
    return _drop_none(payload)


def _monitor_diagnostics_payload(
    order: sqlite3.Row,
    *,
    created_at: str,
    side: str,
    action: str,
    reason: str | None,
    market_status: str | None,
    live_bid: float | None,
    exit_fee_per_contract: float | None,
    net_exit_per_contract: float | None,
    unrealized_pnl: float | None,
    unrealized_roi: float | None,
) -> dict[str, object]:
    return _drop_none(
        {
            "schema_version": 1,
            "kind": "paper_monitor_snapshot",
            "created_at": created_at,
            "order_id": _row_value(order, "id"),
            "target_date": _row_value(order, "target_date"),
            "market_ticker": _row_value(order, "market_ticker"),
            "risk_profile": _row_value(order, "risk_profile"),
            "side": side.upper(),
            "action": action,
            "reason": reason,
            "market_status": market_status,
            "mark": {
                "live_bid": _round_number(live_bid),
                "exit_fee_per_contract": _round_number(exit_fee_per_contract),
                "net_exit_per_contract": _round_number(net_exit_per_contract),
                "unrealized_pnl": _round_number(unrealized_pnl),
                "unrealized_roi": _round_number(unrealized_roi),
            },
            "entry": _order_entry_snapshot(order),
            "entry_diagnostics": _json_object(_row_value(order, "diagnostics_json")),
        }
    )


def _outcome_diagnostics_payload(
    row: sqlite3.Row,
    *,
    event: str,
    resolved_at: str,
    settlement_high_f: float | None,
    resolved_yes: bool | None,
    position_won: bool | None,
    realized_pnl: float,
    exit_price: float | None = None,
    exit_fee_per_contract: float | None = None,
) -> dict[str, object]:
    entry_diagnostics = _json_object(_row_value(row, "diagnostics_json"))
    entry_decision = entry_diagnostics.get("entry_decision") if isinstance(entry_diagnostics, dict) else None
    source_diagnostics = (
        entry_decision.get("diagnostics")
        if isinstance(entry_decision, dict) and isinstance(entry_decision.get("diagnostics"), dict)
        else entry_diagnostics
    )
    prediction_features = (
        source_diagnostics.get("prediction_features")
        if isinstance(source_diagnostics, dict) and isinstance(source_diagnostics.get("prediction_features"), dict)
        else {}
    )
    predicted_high = _optional_float(prediction_features.get("predicted_high_f"))
    forecast_error = (
        settlement_high_f - predicted_high
        if settlement_high_f is not None and predicted_high is not None
        else None
    )
    side = _row_side(row)
    return _drop_none(
        {
            "schema_version": 1,
            "kind": "paper_order_outcome",
            "entry": {
                "order_id": _row_value(row, "id"),
                "decision_snapshot_id": _row_value(row, "entry_decision_snapshot_id"),
                "created_at": _row_value(row, "created_at"),
                "target_date": _row_value(row, "target_date"),
                "market_ticker": _row_value(row, "market_ticker"),
                "label": _row_value(row, "label"),
                "side": side,
                "risk_profile": _row_value(row, "risk_profile"),
                "entry_price": _round_number(_row_value(row, "entry_price")),
                "cost_per_contract": _round_number(_row_value(row, "cost_per_contract")),
                "contracts": _round_number(_row_value(row, "contracts")),
                "probability": _round_number(_row_value(row, "probability")),
                "probability_lcb": _round_number(_row_value(row, "probability_lcb")),
                "edge": _round_number(_row_value(row, "edge")),
                "edge_lcb": _round_number(_row_value(row, "edge_lcb")),
                "trade_quality_score": _round_number(_row_value(row, "trade_quality_score")),
                "reasons": _json_list(_row_value(row, "reasons_json")),
                "diagnostics": source_diagnostics,
            },
            "outcome": {
                "event": event,
                "resolved_at": resolved_at,
                "settlement_high_f": _round_number(settlement_high_f),
                "resolved_yes": resolved_yes,
                "position_won": position_won,
                "realized_pnl": _round_number(realized_pnl),
                "pnl_per_contract": _round_number(
                    realized_pnl / float(_row_value(row, "contracts", 0.0) or 1.0)
                ),
                "exit_price": _round_number(exit_price),
                "exit_fee_per_contract": _round_number(exit_fee_per_contract),
                "forecast_error_f": _round_number(forecast_error),
                "win_loss_reason": _win_loss_reason(
                    event,
                    side=side,
                    resolved_yes=resolved_yes,
                    position_won=position_won,
                    realized_pnl=realized_pnl,
                ),
            },
        }
    )


def _decision_signal_payload(decision: TradeDecision) -> dict[str, object]:
    return _drop_none(
        {
            "ticker": decision.ticker,
            "label": decision.label,
            "action": decision.action,
            "side": decision.side,
            "approved": bool(decision.approved),
            "signal_approved": (
                bool(decision.signal_approved)
                if decision.signal_approved is not None
                else bool(decision.approved)
            ),
            "entry_block_reason": decision.entry_block_reason,
            "probability": _round_number(decision.probability),
            "probability_lcb": _round_number(decision.probability_lcb),
            "model_probability": _round_number(decision.model_probability),
            "market_probability": _round_number(decision.market_probability),
            "residual_probability": _round_number(decision.residual_probability),
            "ensemble_probability": _round_number(decision.ensemble_probability),
            "intraday_probability": _round_number(decision.intraday_probability),
            "remaining_heat_risk": _round_number(decision.remaining_heat_risk),
            "yes_bid": _round_number(decision.yes_bid),
            "yes_ask": _round_number(decision.yes_ask),
            "entry_bid": _round_number(decision.bid),
            "entry_ask": _round_number(decision.ask),
            "entry_bid_size": _round_number(decision.bid_size),
            "entry_ask_size": _round_number(decision.ask_size),
            "spread": _round_number(decision.spread),
            "fee_per_contract": _round_number(decision.fee_per_contract),
            "cost_per_contract": _round_number(decision.cost_per_contract),
            "edge": _round_number(decision.edge),
            "edge_lcb": _round_number(decision.edge_lcb),
            "kelly_fraction": _round_number(decision.kelly_fraction),
            "recommended_contracts": _round_number(decision.recommended_contracts),
            "recommended_spend": _round_number(
                decision.recommended_contracts * decision.cost_per_contract
            ),
            "expected_profit": _round_number(decision.expected_profit),
            "trade_quality_score": _round_number(decision.trade_quality_score),
            "binding_constraint": decision.binding_constraint,
            "strike_type": decision.strike_type,
            "floor_strike": _round_number(decision.floor_strike),
            "cap_strike": _round_number(decision.cap_strike),
            "limit_price": _round_number(decision.limit_price),
            "limit_fee_per_contract": _round_number(decision.limit_fee_per_contract),
            "limit_cost_per_contract": _round_number(decision.limit_cost_per_contract),
            "limit_edge": _round_number(decision.limit_edge),
            "limit_edge_lcb": _round_number(decision.limit_edge_lcb),
            "reasons": list(decision.reasons),
        }
    )


def _forecast_diagnostics_payload(forecast: ForecastSnapshot | None) -> dict[str, object] | None:
    if forecast is None:
        return None
    return _drop_none(
        {
            "target_date": forecast.target_date.isoformat(),
            "predicted_high_f": _round_number(forecast.predicted_high_f),
            "fetched_at": forecast.fetched_at,
            "lead_hours": _round_number(forecast.lead_hours),
            "method": forecast.method,
            "source_spread_f": _round_number(forecast.source_spread_f),
            "source_count": forecast.source_count,
            "sources": {
                "google_high_f": _round_number(forecast.google_high_f),
                "nws_high_f": _round_number(forecast.nws_high_f),
                "open_meteo_high_f": _round_number(forecast.open_meteo_high_f),
                "history_high_f": _round_number(forecast.history_high_f),
            },
            "weights": {
                "google_weight": _round_number(forecast.google_weight),
                "nws_weight": _round_number(forecast.nws_weight),
                "open_meteo_weight": _round_number(forecast.open_meteo_weight),
                "history_weight": _round_number(forecast.history_weight),
            },
            "station_adjustment_f": _round_number(forecast.station_adjustment_f),
            "fresh_station_count": forecast.fresh_station_count,
            "max_calls_per_day": forecast.max_calls_per_day,
            "calls_used_today": forecast.calls_used_today,
            "raw_feature_keys": sorted(forecast.raw.keys()) if isinstance(forecast.raw, dict) else None,
        }
    )


def _intraday_diagnostics_payload(intraday: IntradaySnapshot | None) -> dict[str, object] | None:
    if intraday is None:
        return None
    return _drop_none(
        {
            "target_date": intraday.target_date.isoformat(),
            "observed_high_f": _round_number(intraday.observed_high_f),
            "latest_temp_f": _round_number(intraday.latest_temp_f),
            "latest_observed_at": intraday.latest_observed_at,
            "remaining_forecast_high_f": _round_number(intraday.remaining_forecast_high_f),
            "forecast_fetched_at": intraday.forecast_fetched_at,
            "observation_count": intraday.observation_count,
            "observed_high_source": intraday.observed_high_source,
            "is_complete": intraday.is_complete,
        }
    )


def _market_diagnostics_payload(market, event: EventSnapshot | None) -> dict[str, object] | None:
    if market is None:
        return _drop_none(
            {
                "event_ticker": event.event_ticker if event is not None else None,
                "event_title": event.title if event is not None else None,
                "target_date": event.target_date.isoformat() if event is not None and event.target_date else None,
            }
        )
    return _drop_none(
        {
            "event_ticker": market.event_ticker,
            "event_title": event.title if event is not None else None,
            "ticker": market.ticker,
            "title": market.title,
            "label": market.yes_sub_title,
            "status": market.status,
            "result": market.result,
            "close_time": _market_close_time(market.raw),
            "strike_type": market.strike_type,
            "floor_strike": _round_number(market.floor_strike),
            "cap_strike": _round_number(market.cap_strike),
            "yes_bid": _round_number(market.yes_bid),
            "yes_ask": _round_number(market.yes_ask),
            "no_bid": _round_number(market.no_bid),
            "no_ask": _round_number(market.no_ask),
            "yes_bid_size": _round_number(market.yes_bid_size),
            "yes_ask_size": _round_number(market.yes_ask_size),
            "no_bid_size": _round_number(market.no_bid_size),
            "no_ask_size": _round_number(market.no_ask_size),
            "spread": _round_number(market.spread),
            "no_spread": _round_number(market.no_spread),
            "expiration_value": _round_number(market.expiration_value),
        }
    )


def _market_consensus_diagnostics_payload(
    market_consensus: MarketConsensus | None,
) -> dict[str, object] | None:
    if market_consensus is None:
        return None
    return _drop_none(
        {
            "available": market_consensus.available,
            "implied_high_f": _round_number(market_consensus.implied_high_f),
            "modal_bin_ticker": market_consensus.modal_bin_ticker,
            "modal_bin_label": market_consensus.modal_bin_label,
            "modal_probability": _round_number(market_consensus.modal_probability),
            "implied_stdev_f": _round_number(market_consensus.implied_stdev_f),
            "p10_f": _round_number(market_consensus.p10_f),
            "p25_f": _round_number(market_consensus.p25_f),
            "median_f": _round_number(market_consensus.median_f),
            "p75_f": _round_number(market_consensus.p75_f),
            "p90_f": _round_number(market_consensus.p90_f),
            "overround": _round_number(market_consensus.overround),
            "liquid_bin_count": market_consensus.liquid_bin_count,
        }
    )


def _strategy_config_snapshot(config: StrategyConfig | None) -> dict[str, object] | None:
    if config is None:
        return None
    return {
        key: _json_safe_value(value)
        for key, value in sorted(config.__dict__.items())
    }


def _latest_entry_decision_snapshot(
    conn: sqlite3.Connection,
    target_date: str,
    decision: TradeDecision,
    *,
    risk_profile: str | None,
) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    filters = [
        "target_date = ?",
        "market_ticker = ?",
        "UPPER(COALESCE(side, 'YES')) = ?",
    ]
    params: list[object] = [target_date, decision.ticker, decision.side.upper()]
    if risk_profile is not None:
        filters.append("COALESCE(risk_profile, 'live') = ?")
        params.append(risk_profile)
    return conn.execute(
        f"""
        SELECT *
        FROM decision_snapshots
        WHERE {' AND '.join(filters)}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _entry_decision_ref_payload(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    diagnostics = _json_object(_row_value(row, "diagnostics_json"))
    if not diagnostics:
        diagnostics = {
            "schema_version": 1,
            "kind": "legacy_trade_decision",
            "signal": {
                "approved": bool(_row_value(row, "approved", 0)),
                "signal_approved": bool(
                    _row_value(row, "signal_approved", _row_value(row, "approved", 0))
                ),
                "entry_block_reason": _row_value(row, "entry_block_reason"),
                "probability": _round_number(_row_value(row, "probability")),
                "edge": _round_number(_row_value(row, "edge")),
                "edge_lcb": _round_number(_row_value(row, "edge_lcb")),
                "reasons": _json_list(_row_value(row, "reasons_json")),
            },
        }
    return _drop_none(
        {
            "snapshot_id": int(_row_value(row, "id")),
            "created_at": _row_value(row, "created_at"),
            "approved": bool(_row_value(row, "approved", 0)),
            "signal_approved": bool(
                _row_value(row, "signal_approved", _row_value(row, "approved", 0))
            ),
            "entry_block_reason": _row_value(row, "entry_block_reason"),
            "diagnostics": diagnostics,
        }
    )


def _order_entry_snapshot(row: sqlite3.Row) -> dict[str, object]:
    return _drop_none(
        {
            "entry_decision_snapshot_id": _row_value(row, "entry_decision_snapshot_id"),
            "created_at": _row_value(row, "created_at"),
            "entry_price": _round_number(_row_value(row, "entry_price")),
            "cost_per_contract": _round_number(_row_value(row, "cost_per_contract")),
            "contracts": _round_number(_row_value(row, "contracts")),
            "probability": _round_number(_row_value(row, "probability")),
            "edge": _round_number(_row_value(row, "edge")),
            "edge_lcb": _round_number(_row_value(row, "edge_lcb")),
            "reasons": _json_list(_row_value(row, "reasons_json")),
        }
    )


def _win_loss_reason(
    event: str,
    *,
    side: str,
    resolved_yes: bool | None,
    position_won: bool | None,
    realized_pnl: float,
) -> str:
    if event == "expiration":
        return "Limit order expired unfilled at settlement."
    if event == "close":
        if position_won is True:
            return f"{side} position won because it was closed for positive PnL before settlement."
        if position_won is False:
            return f"{side} position lost because it was closed for negative PnL before settlement."
        return "Position was closed at break-even before settlement."
    if resolved_yes is None or position_won is None:
        return "Outcome was recorded without a resolved market side."
    market_result = "YES" if resolved_yes else "NO"
    verb = "won" if position_won else "lost"
    return f"{side} position {verb} because the market resolved {market_result}."


def _json_object(value: object) -> dict[str, object]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return []


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round_number(value: object) -> float | int | None:
    number = _optional_float(value)
    if number is None:
        return None
    rounded = round(number, 6)
    if rounded.is_integer() and isinstance(value, int):
        return int(rounded)
    return rounded


def _json_safe_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return _round_number(value)
    return str(value)


def _drop_none(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            cleaned_item = _drop_none(item)
            if cleaned_item is not None:
                cleaned[key] = cleaned_item
        return cleaned
    if isinstance(value, list):
        return [_drop_none(item) for item in value if _drop_none(item) is not None]
    return value


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


def _is_pre_resolution_decision(row: sqlite3.Row) -> bool:
    if _row_value(row, "intraday_is_complete", 0):
        return False

    created_at = _parse_timestamp(_row_value(row, "created_at"))
    close_time = _parse_timestamp(_row_value(row, "market_close_time"))
    # Conservative on an unknown close time: a row we cannot prove was written
    # before its market resolved must NOT be scored as a pre-resolution signal,
    # or look-ahead leakage (a decision recorded after the market closed) slips
    # past the guard whenever market_close_time is NULL. Keep only rows we can
    # affirmatively place before close. An undateable row (no created_at, which
    # is NOT NULL in production) keeps the prior lenient default so pathological
    # legacy fixtures are not silently dropped.
    if created_at is None:
        return True
    if close_time is None:
        return False
    return created_at < close_time


def _row_value(row: sqlite3.Row, key: str, default=None):
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _label_resolves_yes(label: str, settlement_high_f: float) -> bool:
    if "or below" in label:
        match = re.search(r"(\d+)", label)
        if not match:
            return False
        return settlement_high_f <= float(match.group(1))
    if "or above" in label:
        match = re.search(r"(\d+)", label)
        if not match:
            return False
        return settlement_high_f >= float(match.group(1))
    match = re.search(r"(\d+).+?(\d+)", label)
    if match:
        lo, hi = float(match.group(1)), float(match.group(2))
        return lo <= settlement_high_f <= hi
    return False


def _row_resolves_yes(row: sqlite3.Row, settlement_high_f: float) -> bool:
    try:
        strike_type = row["strike_type"]
        floor_strike = row["floor_strike"]
        cap_strike = row["cap_strike"]
    except (IndexError, KeyError):
        strike_type = floor_strike = cap_strike = None
    if strike_type:
        normalized = str(strike_type).lower()
        floor_value = float(floor_strike) if floor_strike is not None else None
        cap_value = float(cap_strike) if cap_strike is not None else None
        if normalized == "less":
            return cap_value is not None and settlement_high_f < cap_value
        if normalized == "greater":
            return floor_value is not None and settlement_high_f > floor_value
        if normalized == "between":
            return (
                floor_value is not None
                and cap_value is not None
                and floor_value <= settlement_high_f <= cap_value
            )
    return _label_resolves_yes(row["label"], settlement_high_f)


def _row_side(row: sqlite3.Row) -> str:
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


def _decision_row_resolves_yes(row: sqlite3.Row, settlement_high_f: float) -> bool:
    strike_type = row["strike_type"]
    floor_strike = row["floor_strike"]
    cap_strike = row["cap_strike"]
    if strike_type:
        normalized = str(strike_type).lower()
        floor_value = float(floor_strike) if floor_strike is not None else None
        cap_value = float(cap_strike) if cap_strike is not None else None
        if normalized == "less":
            return cap_value is not None and settlement_high_f < cap_value
        if normalized == "greater":
            return floor_value is not None and settlement_high_f > floor_value
        if normalized == "between":
            return (
                floor_value is not None
                and cap_value is not None
                and floor_value <= settlement_high_f <= cap_value
            )
    return _label_resolves_yes(row["label"], settlement_high_f)


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


def _paper_profile_filter(risk_profile: str | None) -> tuple[str, tuple[str, ...]]:
    if risk_profile is None:
        return "", ()
    return (
        "AND COALESCE(risk_profile, 'live') = ?",
        (normalize_risk_profile_name(risk_profile),),
    )


# One-time rename of stored risk_profile strings written before the 4->2 profile
# collapse, so raw-SQL filters (which compare against the literal new names)
# still match historical AWS paper books. normalize_risk_profile_name() handles
# the read side for any row this misses; this keeps the stored column canonical.
#   balanced, conservative          -> live
#   exploratory, fast-feedback,fast -> research
_LEGACY_PROFILE_RENAMES = {
    "live": ("balanced", "conservative"),
    "research": ("exploratory", "fast-feedback", "fast"),
}
_PROFILE_TABLES = ("paper_orders", "decision_snapshots")


def _migrate_legacy_profile_names(conn: sqlite3.Connection) -> None:
    migration_key = "legacy_profile_names_v2"
    if conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_key=?", (migration_key,)
    ).fetchone() is not None:
        return
    all_legacy = tuple(
        name for names in _LEGACY_PROFILE_RENAMES.values() for name in names
    )
    legacy_placeholders = ",".join("?" for _ in all_legacy)
    for table in _PROFILE_TABLES:
        has_column = any(
            row[1] == "risk_profile"
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if not has_column:
            continue
        # Skip the writes entirely once the table is already migrated -- init()
        # runs on every PaperStore construction (every scan, every 5 min), so the
        # common case must not touch the WAL.
        already_clean = (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE risk_profile IN ({legacy_placeholders}) LIMIT 1",
                all_legacy,
            ).fetchone()
            is None
        )
        if already_clean:
            continue
        for new_name, legacy_names in _LEGACY_PROFILE_RENAMES.items():
            placeholders = ",".join("?" for _ in legacy_names)
            conn.execute(
                f"UPDATE {table} SET risk_profile = ? "
                f"WHERE risk_profile IN ({placeholders})",
                (new_name, *legacy_names),
            )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (migration_key, completed_at) VALUES (?, ?)",
        (migration_key, _now()),
    )
