"""Durable record and account bootstrap for the M1 market-recalibration trial.

Production prunes ``decision_snapshots`` after about a day, so the trial keeps
its own evidence: every sampled evaluation of every day-ahead leg, the first
crossing of each shadow arm, the final CLI highs that resolve recorded legs,
and one reconciliation report per UTC day. Nothing here is read by any other
account's trading path.

The account itself is an ordinary paper ledger: one ``paper_accounts`` row and
one ``OPENING_CASH`` event, both idempotent, so every account-scoped surface
(settlement, ledger reconciliation, publication) works unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..research_m1 import M1_ACCOUNT_ID, M1_POLICY


M1_TRIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS m1_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    utc_hour TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    coefficient_set_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    civil_tz TEXT NOT NULL,
    local_hour REAL NOT NULL,
    lead_days INTEGER NOT NULL,
    listing_hour INTEGER NOT NULL,
    bucket TEXT NOT NULL,
    market_probability REAL,
    q_yes REAL,
    p_yes REAL,
    p_trade REAL,
    model_probability REAL,
    ask REAL,
    bid REAL,
    ask_size REAL,
    bid_size REAL,
    spread_cents INTEGER,
    gate_fee REAL,
    cost REAL,
    edge REAL,
    agree INTEGER,
    model_market_gap REAL,
    market_status TEXT,
    market_close_time TEXT,
    forecast_age_hours REAL,
    source_spread_f REAL,
    entry_gate_reason TEXT,
    leg_passes INTEGER NOT NULL,
    contracts INTEGER NOT NULL,
    stake REAL NOT NULL,
    placement_enabled INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    order_id INTEGER
);
-- One sampled evaluation per leg per UTC hour: the first research-profile
-- scan that evaluates the leg in the hour wins, and the database enforces it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_m1_evaluations_leg_hour
    ON m1_evaluations (target_date, market_ticker, side, utc_hour);
CREATE INDEX IF NOT EXISTS idx_m1_evaluations_created
    ON m1_evaluations (created_at);
CREATE INDEX IF NOT EXISTS idx_m1_evaluations_target_series
    ON m1_evaluations (target_date, series_ticker);
CREATE INDEX IF NOT EXISTS idx_m1_evaluations_order
    ON m1_evaluations (order_id) WHERE order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS m1_shadow_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    arm TEXT NOT NULL,
    created_at TEXT NOT NULL,
    utc_hour TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    coefficient_set_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    bucket TEXT NOT NULL,
    q_yes REAL NOT NULL,
    p_trade REAL NOT NULL,
    cost REAL NOT NULL,
    edge REAL NOT NULL,
    ask REAL NOT NULL,
    bid REAL NOT NULL,
    ask_size REAL NOT NULL,
    model_probability REAL,
    agree INTEGER,
    contracts INTEGER NOT NULL,
    stake REAL NOT NULL,
    UNIQUE (arm, target_date, market_ticker, side)
);

CREATE TABLE IF NOT EXISTS m1_settlement_truth (
    series_ticker TEXT NOT NULL,
    target_date TEXT NOT NULL,
    settlement_high_f REAL NOT NULL,
    truth_source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (series_ticker, target_date)
);

CREATE TABLE IF NOT EXISTS m1_reconciliations (
    utc_day TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    evaluations INTEGER NOT NULL,
    entries INTEGER NOT NULL,
    matched INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    missed INTEGER NOT NULL,
    breaches INTEGER NOT NULL,
    contradictions INTEGER NOT NULL,
    report_json TEXT NOT NULL
);
"""

# Database backstop for "at most one entry per (target date, market, side),
# ever": a second M1 root order for a leg cannot be inserted by any path.
M1_LEG_ONCE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_orders_m1_leg_once
    ON paper_orders (
        target_date,
        market_ticker,
        UPPER(COALESCE(side, 'YES'))
    )
    WHERE account_id = '{M1_ACCOUNT_ID}'
      AND parent_order_id IS NULL
      AND status != 'REJECTED'
"""

M1_TRIAL_TABLES = (
    "m1_evaluations",
    "m1_shadow_entries",
    "m1_settlement_truth",
    "m1_reconciliations",
)


def ensure_m1_trial_schema(conn: sqlite3.Connection) -> None:
    """Create the trial's tables and the one-entry-per-leg index (idempotent)."""

    conn.executescript(M1_TRIAL_SCHEMA)
    conn.execute(M1_LEG_ONCE_INDEX)


def ensure_m1_trial_account(conn: sqlite3.Connection) -> None:
    """Bootstrap the separate $2,000 paper ledger exactly once.

    ``INSERT OR IGNORE`` plus the fixed idempotency key make concurrent or
    repeated initialization safe. An account a previous deployment archived is
    left archived: admission then fails closed until an operator decides.
    """

    created_at = datetime.now(UTC).isoformat()
    equity = float(M1_POLICY.reference_equity)
    conn.execute(
        "INSERT OR IGNORE INTO paper_accounts "
        "(account_id, created_at, initial_capital, opening_cash, "
        "high_water_equity, status, cutover_note) "
        "VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)",
        (
            M1_ACCOUNT_ID,
            created_at,
            equity,
            equity,
            equity,
            (
                "isolated M1 market-recalibration paper trial "
                f"({M1_POLICY.policy_version}); paper only"
            ),
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO paper_account_ledger "
        "(created_at, account_id, order_id, event_type, amount, "
        "idempotency_key, details_json) VALUES (?, ?, NULL, 'OPENING_CASH', ?, ?, ?)",
        (
            created_at,
            M1_ACCOUNT_ID,
            equity,
            f"{M1_ACCOUNT_ID}:opening",
            json.dumps(
                {
                    "coefficient_set_id": M1_POLICY.coefficient_set_id,
                    "paper_only": True,
                    "policy_fingerprint": M1_POLICY.policy_fingerprint,
                    "policy_version": M1_POLICY.policy_version,
                },
                sort_keys=True,
            ),
        ),
    )
