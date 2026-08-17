from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from ..account import LIVE_STABILITY_ACCOUNT_ID
from .market_day_settlements import (
    MARKET_DAY_SETTLEMENT_INDEXES,
    MARKET_DAY_SETTLEMENT_SCHEMA,
)
from ..research_policy import (
    ALL_RESEARCH_POLICIES,
    TARGET_POLICY,
    TARGET_POLICY_V1,
)

try:
    import fcntl
except ImportError:  # pragma: no cover -- POSIX-only production/dev hosts
    fcntl = None

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _exclusive_init_lock(db_path: object):
    """Serialize the whole init/migration/bootstrap path (audit DB-01).

    Five+ systemd units initialize the same store concurrently. SQLite's
    busy handler does not protect the multi-statement init sequence: two
    initializers racing between ``ALTER TABLE``/``PRAGMA table_info`` raise
    "database schema has changed", and the SELECT-then-INSERT account
    bootstrap raced to a UNIQUE violation. An exclusive advisory file lock
    beside the database serializes initializers across processes AND threads
    (each holder opens its own file descriptor) without changing SQLite
    transaction semantics for non-init writers.
    """

    path = str(db_path or "")
    if fcntl is None or not path or path == ":memory:":
        yield
        return
    try:
        handle = open(path + ".init.lock", "a+")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


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

CREATE TABLE IF NOT EXISTS market_orderbook_depth_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    scan_run_id TEXT,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    risk_profile TEXT,
    levels_requested INTEGER NOT NULL,
    yes_levels_json TEXT NOT NULL,
    no_levels_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orderbook_depth_ticker_time
ON market_orderbook_depth_snapshots(market_ticker, created_at);

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

CREATE TABLE IF NOT EXISTS scan_context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    risk_profile TEXT,
    station_id TEXT,
    event_ticker TEXT,
    bankroll REAL,
    forecast_snapshot_id INTEGER REFERENCES forecast_snapshots(id),
    market_snapshot_id INTEGER REFERENCES market_snapshots(id),
    forecast_json TEXT,
    intraday_json TEXT,
    market_json TEXT,
    market_consensus_json TEXT,
    prediction_features_json TEXT NOT NULL,
    strategy_config_json TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    -- Content identity of the point-in-time observation, deliberately
    -- excluding risk profile/bankroll/sleeve/account identity so the exact
    -- same observation can feed live, target, and motion decisions without
    -- a duplicated, insertion-order-biased context row (chronological
    -- research tuning Task 1).
    source_context_hash TEXT,
    source_scan_run_id TEXT
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
    scan_context_id INTEGER REFERENCES scan_context_snapshots(id),
    prediction_features_json TEXT,
    diagnostics_json TEXT,
    reasons_json TEXT NOT NULL,
    -- Identity of the policy that produced this specific decision row.
    -- Profile/sleeve/bankroll identity belongs to decision rows, never to
    -- the shared source-neutral scan context above.
    decision_policy_fingerprint TEXT
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

CREATE TABLE IF NOT EXISTS maker_volume_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    UNIQUE (trade_id, order_id)
);

CREATE TABLE IF NOT EXISTS dataset_kalshi_trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    created_time TEXT NOT NULL,
    count REAL,
    yes_price REAL,
    no_price REAL,
    is_block_trade INTEGER NOT NULL DEFAULT 0,
    taker_book_side TEXT,
    maker_side TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_maker_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    execution_model_version TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    order_id INTEGER NOT NULL REFERENCES paper_orders(id),
    trade_created_at TEXT NOT NULL,
    maker_side TEXT NOT NULL,
    side_price REAL NOT NULL,
    queue_quantity REAL NOT NULL DEFAULT 0,
    fill_quantity REAL NOT NULL DEFAULT 0,
    counterfactual INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL,
    UNIQUE (execution_model_version, order_id, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_maker_allocations_ticker_trade
ON paper_maker_allocations (market_ticker, trade_id, counterfactual);

CREATE TABLE IF NOT EXISTS paper_settlement_verifications (
    order_id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    target_date TEXT NOT NULL,
    booked_high_f REAL NOT NULL,
    final_high_f REAL,
    verification_status TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS research_daily_goals (
    objective_day TEXT NOT NULL,
    account_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reference_equity REAL NOT NULL CHECK(reference_equity > 0),
    target_return REAL NOT NULL CHECK(target_return > 0),
    target_pnl REAL NOT NULL CHECK(target_pnl > 0),
    PRIMARY KEY(objective_day, account_id, policy_version)
);

CREATE TABLE IF NOT EXISTS research_plan_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    objective_day TEXT NOT NULL,
    scan_run_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    target_pnl REAL NOT NULL,
    realized_today REAL NOT NULL,
    remaining_target REAL NOT NULL CHECK(remaining_target >= 0),
    available_conservative_expected_profit REAL NOT NULL
        CHECK(available_conservative_expected_profit >= 0),
    target_feasible INTEGER NOT NULL CHECK(target_feasible IN (0, 1))
);

-- Immutable declaration of one predeclared research candidate. Declared
-- before any evaluation window (enforced by a trigger on research_evidence
-- below), and never editable or deletable once written, so a challenger's
-- parameters are always fixed before the test fold that scores it exists.
CREATE TABLE IF NOT EXISTS research_experiments (
    experiment_id TEXT PRIMARY KEY,
    declared_at TEXT NOT NULL,
    hypothesis_family TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    parameter_json TEXT NOT NULL,
    evidence_role TEXT NOT NULL CHECK(evidence_role IN ('exploratory','confirmatory')),
    UNIQUE(hypothesis_family, candidate_key, candidate_version)
);

-- Immutable paired baseline/challenger fold score. Only normalized
-- market/forecast input and derived challenger evidence belong here --
-- never raw, expiring third-party content.
CREATE TABLE IF NOT EXISTS research_evidence (
    experiment_id TEXT NOT NULL REFERENCES research_experiments(experiment_id),
    fold_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    challenger_json TEXT NOT NULL,
    PRIMARY KEY(experiment_id, fold_id, station_id, target_date)
);

-- Task 7: one immutable paired baseline/Google-challenger fold record, so
-- the fixed research challenger can be scored after settlement even though
-- the raw Google runtime evidence it was derived from has already expired.
-- Only derived mean/sigma/bracket-probability/action values belong here --
-- never a raw Google high, gap, response body, conditions text, URL, key,
-- or token (spec section 7.2/7.3); PaperStore.record_google_challenger_snapshot
-- enforces that on write.
CREATE TABLE IF NOT EXISTS google_challenger_snapshots (
  station_id TEXT NOT NULL,
  target_date TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  baseline_mu REAL NOT NULL,
  baseline_sigma REAL NOT NULL,
  challenger_mu REAL,
  challenger_sigma REAL NOT NULL,
  baseline_probabilities_json TEXT NOT NULL,
  challenger_probabilities_json TEXT,
  action TEXT NOT NULL,
  PRIMARY KEY(station_id, target_date, issued_at, policy_version)
);
""" + MARKET_DAY_SETTLEMENT_SCHEMA

# Created after column migrations in init() so they can reference late-added
# columns (e.g. group_id) on databases that predate them.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_paper_orders_market_side
    ON paper_orders (target_date, market_ticker, side, status);
CREATE INDEX IF NOT EXISTS idx_paper_orders_lifecycle
    ON paper_orders (status, settled_at, closed_at);
CREATE INDEX IF NOT EXISTS idx_paper_orders_group
    ON paper_orders (group_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_parent
    ON paper_orders (parent_order_id, id)
    WHERE parent_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_orders_account_closed
    ON paper_orders (account_id, closed_at, id)
    WHERE status != 'REJECTED' AND closed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_orders_account_settled
    ON paper_orders (account_id, settled_at, id)
    WHERE status != 'REJECTED' AND settled_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_orders_account_expires
    ON paper_orders (account_id, expires_at, id)
    WHERE status != 'REJECTED' AND expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_orders_expired_created
    ON paper_orders (account_id, created_at, id)
    WHERE status = 'PAPER_EXPIRED' AND expires_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_market
    ON decision_snapshots (target_date, market_ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_target
    ON market_snapshots (target_date, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_scan_context
    ON decision_snapshots (scan_context_id)
    WHERE scan_context_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_context_snapshots_source_hash
    ON scan_context_snapshots (source_context_hash)
    WHERE source_context_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_probability_snapshots_market
    ON probability_snapshots (target_date, market_ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_monitor_snapshots_order
    ON paper_monitor_snapshots (order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_research_plan_snapshots_current
    ON research_plan_snapshots (
        objective_day, account_id, policy_version, created_at, id
    );
CREATE INDEX IF NOT EXISTS idx_research_shadow_orders_target
    ON research_shadow_orders (target_date, market_ticker, side, created_at);
CREATE INDEX IF NOT EXISTS idx_research_shadow_orders_link
    ON research_shadow_orders (linked_paper_order_id);
CREATE INDEX IF NOT EXISTS idx_research_shadow_monitor_order
    ON research_shadow_monitor_snapshots (shadow_order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_account_ledger_account
    ON paper_account_ledger (account_id, created_at, id);
""" + MARKET_DAY_SETTLEMENT_INDEXES

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
DECISION_SNAPSHOT_PENDING_RESEARCH_INDEX = """
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_pending_research_admission
    ON decision_snapshots (id)
    WHERE research_sleeve IS NOT NULL
      AND approved = 0
      AND entry_block_reason = 'research admission pending'
"""

# Retention indexes (2026-07-27 audit, finding F.8). The nightly prune's
# anti-join subqueries had no index to stand on, which is why it burned 21
# minutes of wall clock for 5 minutes of CPU and timed out every night:
#
#   * the dedup grouping needs to resolve MAX(id) per group by index scan
#     instead of a full-table temp B-tree. The index must be declared on the
#     EXPRESSION the query groups by -- COALESCE(risk_profile, '') -- not on the
#     bare column: SQLite will not match a plain-column index to an expression,
#     and silently falls back to USE TEMP B-TREE FOR GROUP BY, which is the very
#     full-table sort this index exists to remove. `id` sits immediately after
#     the group keys so the "is there a newer row in this group" probe is a
#     range seek, and created_at trails so the window filter is covered without
#     a table lookup. target_date leads deliberately: with market_ticker in
#     front this index outranked the covering created_at index for the
#     cities-report `GROUP BY market_ticker` aggregation and turned that
#     report's date-range seek into a full index scan. The probe constrains all
#     four group keys by equality, so their relative order costs it nothing;
#   * the forecast/market parent-orphan probes are correlated EXISTS, so each
#     runs once per candidate row and touches BOTH referencing tables. All four
#     FK columns involved -- decision_snapshots.{forecast,market}_snapshot_id
#     and scan_context_snapshots.{forecast,market}_snapshot_id -- carried no
#     index, so every probe degenerated to a full scan of the probed table for
#     each candidate row. Indexing only the decision_snapshots side is not
#     enough; both sides are probed;
#   * forecast_snapshots carried no index whatsoever, so both the prune cutoff
#     and archive.gate_missing_days' per-day scan were full scans.
#
# These follow the same deliberate out-of-band contract as the report index
# above: fresh databases build them cheaply at init, existing journals get them
# once from deploy/aws/create_retention_indexes.sh while scanners are paused.
# Never add these to the always-on SCHEMA block -- on the current 10.9 GB
# production journal these are multi-minute builds.
DECISION_SNAPSHOT_RETENTION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_retention_dedup
    ON decision_snapshots (
        target_date, market_ticker, side, COALESCE(risk_profile, ''),
        id, created_at
    );
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_forecast_parent
    ON decision_snapshots (forecast_snapshot_id)
    WHERE forecast_snapshot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_market_parent
    ON decision_snapshots (market_snapshot_id)
    WHERE market_snapshot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_scan_context_snapshots_forecast_parent
    ON scan_context_snapshots (forecast_snapshot_id)
    WHERE forecast_snapshot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_scan_context_snapshots_market_parent
    ON scan_context_snapshots (market_snapshot_id)
    WHERE market_snapshot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_created
    ON forecast_snapshots (created_at, id);
CREATE INDEX IF NOT EXISTS idx_probability_snapshots_created
    ON probability_snapshots (created_at, id);
CREATE INDEX IF NOT EXISTS idx_monitor_snapshots_created
    ON paper_monitor_snapshots (created_at, id);
CREATE INDEX IF NOT EXISTS idx_scan_context_snapshots_created
    ON scan_context_snapshots (created_at, id);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_created
    ON market_snapshots (created_at, id);
"""

# Canonical name list for the out-of-band builder and the init-time warning, so
# the deploy script and the runtime check can never drift from the definitions.
RETENTION_INDEX_NAMES = (
    "idx_decision_snapshots_retention_dedup",
    "idx_decision_snapshots_forecast_parent",
    "idx_decision_snapshots_market_parent",
    "idx_scan_context_snapshots_forecast_parent",
    "idx_scan_context_snapshots_market_parent",
    "idx_forecast_snapshots_created",
    "idx_probability_snapshots_created",
    "idx_monitor_snapshots_created",
    "idx_scan_context_snapshots_created",
    "idx_market_snapshots_created",
)

# DB-level backstop for the application's concurrent-open guard. Account is the
# isolation boundary: target and motion may independently hold the same side of
# one market, while duplicate active exposure inside either account is forbidden.
# Keep the historical object name so operational scripts that temporarily drop
# the guard continue to work; the indexed identity is deliberately account-, not
# profile-, scoped. NULL pre-account rows retain shared-book semantics.
OPEN_POSITION_GUARD_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_orders_open_market_side_profile
    ON paper_orders (
        COALESCE(account_id, 'paper-shared'),
        target_date,
        market_ticker,
        UPPER(COALESCE(side, 'YES'))
    )
    WHERE status IN (
        'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
        'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
    )
      AND settled_at IS NULL
      AND closed_at IS NULL
"""

_OPEN_POSITION_GUARD_NAME = "ux_paper_orders_open_market_side_profile"


def _normalized_sql_tokens(sql: str) -> tuple[str, ...]:
    """Return a whitespace-insensitive, literal-preserving SQL token stream."""

    tokens = re.findall(
        r"'(?:''|[^'])*'|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|"
        r"<>|!=|<=|>=|==|[(),.;=<>+*/%.-]",
        sql,
    )
    normalized = [token if token.startswith("'") else token.upper() for token in tokens]
    for index in range(len(normalized) - 2):
        if normalized[index : index + 3] == ["IF", "NOT", "EXISTS"]:
            del normalized[index : index + 3]
            break
    return tuple(normalized)


_CANONICAL_OPEN_POSITION_GUARD_TOKENS = _normalized_sql_tokens(
    OPEN_POSITION_GUARD_INDEX
)


def _open_position_guard_is_canonical(
    conn: sqlite3.Connection,
    stored_sql: str,
) -> bool:
    """Verify the complete guard definition and SQLite's realized metadata."""

    if _normalized_sql_tokens(stored_sql) != _CANONICAL_OPEN_POSITION_GUARD_TOKENS:
        return False
    index_row = next(
        (
            row
            for row in conn.execute("PRAGMA index_list(paper_orders)").fetchall()
            if str(row[1]) == _OPEN_POSITION_GUARD_NAME
        ),
        None,
    )
    if index_row is None:
        return False
    # seq, name, unique, origin, partial
    if int(index_row[2]) != 1 or str(index_row[3]) != "c" or int(index_row[4]) != 1:
        return False
    xinfo = conn.execute(
        f"PRAGMA index_xinfo({_OPEN_POSITION_GUARD_NAME})"
    ).fetchall()
    # Two expressions surround the exact target/ticker columns; the final row
    # is SQLite's non-key rowid payload.
    return (
        [int(row[1]) for row in xinfo] == [-2, 2, 3, -2, -1]
        and [row[2] for row in xinfo]
        == [None, "target_date", "market_ticker", None, None]
        and [int(row[5]) for row in xinfo] == [1, 1, 1, 1, 0]
    )

RESEARCH_IDENTITY_COLUMNS = {
    "account_id": "TEXT",
    "research_sleeve": "TEXT",
    "research_policy_version": "TEXT",
    "policy_fingerprint": "TEXT",
    "objective_day": "TEXT",
    "lead_bucket": "TEXT",
    "scan_run_id": "TEXT",
    "reentry_fingerprint": "TEXT",
}

RESEARCH_DAILY_GOAL_AUDIT_COLUMNS = {
    "policy_fingerprint": "TEXT",
}

PAPER_ORDER_AUDIT_COLUMNS = {
    "settled_at": "TEXT",
    "settlement_high_f": "REAL",
    "resolved_yes": "INTEGER",
    # The market's own outcome ("the bin resolved YES"), written only when a
    # settlement high is known. Never a restatement of this position's P&L.
    "realized_pnl": "REAL",
    # Whether THIS position won, which for an early close is a P&L fact and not
    # a market fact. Split out of resolved_yes so neither field has to lie:
    # see _migrate_closed_row_position_won below.
    "position_won": "INTEGER",
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
    "requested_contracts": "REAL",
    "filled_contracts": "REAL",
    "remaining_contracts": "REAL",
    "queue_remaining": "REAL",
    "execution_model_version": "TEXT",
    # Depth-aware partial closes (audit EX-02): the executed slice of a partial
    # close becomes its own PAPER_CLOSED row linked back to the original order.
    "parent_order_id": "INTEGER",
    **RESEARCH_IDENTITY_COLUMNS,
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
    "scan_context_id": "INTEGER REFERENCES scan_context_snapshots(id)",
    "prediction_features_json": "TEXT",
    "diagnostics_json": "TEXT",
    "signal_approved": "INTEGER",
    "entry_block_reason": "TEXT",
    "decision_policy_fingerprint": "TEXT",
    **RESEARCH_IDENTITY_COLUMNS,
}

RESEARCH_SHADOW_AUDIT_COLUMNS = {
    "entry_decision_snapshot_id": "INTEGER",
    "diagnostics_json": "TEXT",
    "outcome_diagnostics_json": "TEXT",
}

MONITOR_AUDIT_COLUMNS = {
    "diagnostics_json": "TEXT",
    **RESEARCH_IDENTITY_COLUMNS,
}

SCAN_CONTEXT_AUDIT_COLUMNS = {
    "created_at": "TEXT",
    "target_date": "TEXT",
    "risk_profile": "TEXT",
    "station_id": "TEXT",
    "event_ticker": "TEXT",
    "bankroll": "REAL",
    "forecast_snapshot_id": "INTEGER REFERENCES forecast_snapshots(id)",
    "market_snapshot_id": "INTEGER REFERENCES market_snapshots(id)",
    "forecast_json": "TEXT",
    "intraday_json": "TEXT",
    "market_json": "TEXT",
    "market_consensus_json": "TEXT",
    "prediction_features_json": "TEXT",
    "strategy_config_json": "TEXT",
    "schema_version": "INTEGER",
    "source_context_hash": "TEXT",
    "source_scan_run_id": "TEXT",
    **RESEARCH_IDENTITY_COLUMNS,
}


_RESEARCH_IDENTITY_TABLES = (
    "paper_orders",
    "decision_snapshots",
    "scan_context_snapshots",
    "paper_monitor_snapshots",
    "research_shadow_monitor_snapshots",
)


def _research_identity_trigger_sql(table: str, operation: str) -> tuple[str, str]:
    """Generate one canonical research-identity trigger definition."""

    if table not in _RESEARCH_IDENTITY_TABLES:
        raise ValueError(f"unsupported research identity table: {table}")
    if operation not in {"INSERT", "UPDATE"}:
        raise ValueError(f"unsupported research identity operation: {operation}")
    identity_markers = tuple(
        column for column in RESEARCH_IDENTITY_COLUMNS if column != "account_id"
    )
    new_identity = " OR ".join(
        f"NEW.{column} IS NOT NULL" for column in identity_markers
    )
    old_identity = " OR ".join(
        f"OLD.{column} IS NOT NULL" for column in identity_markers
    )
    missing_core = " OR ".join(
        f"NULLIF(TRIM(NEW.{column}), '') IS NULL"
        for column in (
            "account_id",
            "research_sleeve",
            "research_policy_version",
            "policy_fingerprint",
        )
    )
    research_account_ids = ", ".join(
        repr(policy.account_id) for policy in ALL_RESEARCH_POLICIES
    )
    new_research_account = f"NEW.account_id IN ({research_account_ids})"
    old_research_account = f"OLD.account_id IN ({research_account_ids})"
    trigger = f"trg_{table}_research_identity_{operation.lower()}"
    if operation == "INSERT":
        account_condition = (
            f"{new_research_account} OR " if table == "paper_orders" else ""
        )
        established_identity = f"{account_condition}{new_identity}"
    else:
        account_condition = (
            f"{old_research_account} OR {new_research_account} OR "
            if table == "paper_orders"
            else ""
        )
        established_identity = f"{account_condition}{old_identity} OR {new_identity}"
    condition = f"({established_identity}) AND ({missing_core})"
    sql = f"""
        CREATE TRIGGER {trigger}
        BEFORE {operation} ON {table}
        WHEN {condition}
        BEGIN
            SELECT RAISE(
                ABORT,
                'research identity requires sleeve, policy version, and fingerprint'
            );
        END
    """
    return trigger, sql


def _ensure_research_identity_triggers(conn: sqlite3.Connection) -> None:
    """Require complete identity on new sleeve evidence, never legacy rows."""

    for table in _RESEARCH_IDENTITY_TABLES:
        for operation in ("INSERT", "UPDATE"):
            trigger, expected_sql = _research_identity_trigger_sql(table, operation)
            stored = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger,),
            ).fetchone()
            if (
                stored is not None
                and _normalized_sql_tokens(str(stored[0] or ""))
                == _normalized_sql_tokens(expected_sql)
            ):
                continue
            # Trigger names are schema API. Replace only stale definitions;
            # clean init stays read-only while mismatches rebuild transactionally.
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute(expected_sql)


def _ensure_research_daily_goal_triggers(conn: sqlite3.Connection) -> None:
    """Make frozen objective evidence append-only, including old databases."""

    definitions = {
        "trg_research_daily_goals_immutable_update": """
            CREATE TRIGGER trg_research_daily_goals_immutable_update
            BEFORE UPDATE ON research_daily_goals
            BEGIN
                SELECT RAISE(ABORT, 'research daily goals are immutable');
            END
        """,
        "trg_research_daily_goals_immutable_delete": """
            CREATE TRIGGER trg_research_daily_goals_immutable_delete
            BEFORE DELETE ON research_daily_goals
            BEGIN
                SELECT RAISE(ABORT, 'research daily goals are immutable');
            END
        """,
        "trg_research_plan_snapshots_immutable_update": """
            CREATE TRIGGER trg_research_plan_snapshots_immutable_update
            BEFORE UPDATE ON research_plan_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'research plan snapshots are immutable');
            END
        """,
        "trg_research_plan_snapshots_immutable_delete": """
            CREATE TRIGGER trg_research_plan_snapshots_immutable_delete
            BEFORE DELETE ON research_plan_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'research plan snapshots are immutable');
            END
        """,
    }
    for trigger, expected_sql in definitions.items():
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()
        if (
            stored is not None
            and _normalized_sql_tokens(str(stored[0] or ""))
            == _normalized_sql_tokens(expected_sql)
        ):
            continue
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(expected_sql)


def _ensure_research_experiment_triggers(conn: sqlite3.Connection) -> None:
    """Freeze declared research experiments and their paired evidence.

    A challenger's parameters must be fixed before any test-fold outcome can
    be scored against it, so both tables are append-only from the moment a
    row lands, and evidence can only reference an experiment declared before
    the day it evaluates -- a challenger can never be tuned to, or selected
    by, the days it is later scored on.

    ``INSERT OR REPLACE`` (and ``INSERT ... ON CONFLICT DO UPDATE`` routed
    through REPLACE conflict resolution) deletes the conflicting row
    internally before reinserting it. With ``PRAGMA recursive_triggers`` off
    (the SQLite default, unchanged here), that internal delete does not fire
    the BEFORE DELETE/UPDATE triggers above, so a REPLACE could silently
    rewrite an "immutable" row without ever tripping them. The two
    immutable-insert triggers below close that gap: a BEFORE INSERT trigger
    runs before REPLACE's conflict resolution removes the existing row, so
    the conflicting row is still visible to ``WHEN EXISTS`` and the whole
    statement aborts instead of the trigger-less internal delete silently
    succeeding.
    """

    definitions = {
        "trg_research_experiments_immutable_update": """
            CREATE TRIGGER trg_research_experiments_immutable_update
            BEFORE UPDATE ON research_experiments
            BEGIN
                SELECT RAISE(ABORT, 'research experiments are immutable');
            END
        """,
        "trg_research_experiments_immutable_delete": """
            CREATE TRIGGER trg_research_experiments_immutable_delete
            BEFORE DELETE ON research_experiments
            BEGIN
                SELECT RAISE(ABORT, 'research experiments are immutable');
            END
        """,
        "trg_research_experiments_immutable_insert": """
            CREATE TRIGGER trg_research_experiments_immutable_insert
            BEFORE INSERT ON research_experiments
            WHEN EXISTS (
                SELECT 1 FROM research_experiments
                WHERE experiment_id = NEW.experiment_id
                   OR (
                       hypothesis_family = NEW.hypothesis_family
                       AND candidate_key = NEW.candidate_key
                       AND candidate_version = NEW.candidate_version
                   )
            )
            BEGIN
                SELECT RAISE(ABORT, 'research experiments are immutable');
            END
        """,
        "trg_research_evidence_immutable_update": """
            CREATE TRIGGER trg_research_evidence_immutable_update
            BEFORE UPDATE ON research_evidence
            BEGIN
                SELECT RAISE(ABORT, 'research evidence is immutable');
            END
        """,
        "trg_research_evidence_immutable_delete": """
            CREATE TRIGGER trg_research_evidence_immutable_delete
            BEFORE DELETE ON research_evidence
            BEGIN
                SELECT RAISE(ABORT, 'research evidence is immutable');
            END
        """,
        "trg_research_evidence_immutable_insert": """
            CREATE TRIGGER trg_research_evidence_immutable_insert
            BEFORE INSERT ON research_evidence
            WHEN EXISTS (
                SELECT 1 FROM research_evidence
                WHERE experiment_id = NEW.experiment_id
                  AND fold_id = NEW.fold_id
                  AND station_id = NEW.station_id
                  AND target_date = NEW.target_date
            )
            BEGIN
                SELECT RAISE(ABORT, 'research evidence is immutable');
            END
        """,
        "trg_research_evidence_declared_before_window": """
            CREATE TRIGGER trg_research_evidence_declared_before_window
            BEFORE INSERT ON research_evidence
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'research evidence must be declared before its evaluation window'
                )
                WHERE NOT EXISTS (
                    SELECT 1 FROM research_experiments
                    WHERE experiment_id = NEW.experiment_id
                      AND datetime(declared_at) <= datetime(NEW.evaluated_at)
                      AND date(declared_at) <= date(NEW.target_date)
                );
            END
        """,
    }
    for trigger, expected_sql in definitions.items():
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()
        if (
            stored is not None
            and _normalized_sql_tokens(str(stored[0] or ""))
            == _normalized_sql_tokens(expected_sql)
        ):
            continue
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(expected_sql)


def _backfill_legacy_research_daily_goal_fingerprints(
    conn: sqlite3.Connection,
) -> None:
    """Stamp only legacy rows that exactly prove the frozen v1 policy."""

    eligible = conn.execute(
        "SELECT 1 FROM research_daily_goals "
        "WHERE (policy_fingerprint IS NULL OR TRIM(policy_fingerprint)='') "
        "AND account_id=? AND policy_version=? "
        "AND typeof(reference_equity) IN ('integer', 'real') "
        "AND typeof(target_return) IN ('integer', 'real') "
        "AND typeof(target_pnl) IN ('integer', 'real') "
        "AND reference_equity=? AND target_return=? AND target_pnl=? LIMIT 1",
        (
            TARGET_POLICY_V1.account_id,
            TARGET_POLICY_V1.policy_version,
            TARGET_POLICY_V1.reference_equity,
            TARGET_POLICY_V1.target_return,
            TARGET_POLICY_V1.target_pnl,
        ),
    ).fetchone()
    if eligible is None:
        return
    # An intermediate schema may already have installed the append-only
    # trigger before it learned how to stamp legacy rows. Initialization is one
    # transaction, so dropping and canonical recreation are rollback-safe.
    conn.execute(
        "DROP TRIGGER IF EXISTS trg_research_daily_goals_immutable_update"
    )
    conn.execute(
        "UPDATE research_daily_goals SET policy_fingerprint=? "
        "WHERE (policy_fingerprint IS NULL OR TRIM(policy_fingerprint)='') "
        "AND account_id=? AND policy_version=? "
        "AND typeof(reference_equity) IN ('integer', 'real') "
        "AND typeof(target_return) IN ('integer', 'real') "
        "AND typeof(target_pnl) IN ('integer', 'real') "
        "AND reference_equity=? AND target_return=? AND target_pnl=?",
        (
            TARGET_POLICY_V1.policy_fingerprint,
            TARGET_POLICY_V1.account_id,
            TARGET_POLICY_V1.policy_version,
            TARGET_POLICY_V1.reference_equity,
            TARGET_POLICY_V1.target_return,
            TARGET_POLICY_V1.target_pnl,
        ),
    )


def _ensure_research_sleeve_accounts(conn: sqlite3.Connection) -> None:
    """Bootstrap all ledgers and archive every policy except active target v3."""

    for policy in ALL_RESEARCH_POLICIES:
        created_at = _now()
        conn.execute(
            "INSERT OR IGNORE INTO paper_accounts "
            "(account_id, created_at, initial_capital, opening_cash, "
            "high_water_equity, status, cutover_note) "
            "VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)",
            (
                policy.account_id,
                created_at,
                policy.reference_equity,
                policy.reference_equity,
                policy.reference_equity,
                f"isolated {policy.sleeve.value} research ledger ({policy.policy_version})",
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO paper_account_ledger "
            "(created_at, account_id, order_id, event_type, amount, "
            "idempotency_key, details_json) VALUES (?, ?, NULL, 'OPENING_CASH', ?, ?, NULL)",
            (
                created_at,
                policy.account_id,
                policy.reference_equity,
                f"{policy.account_id}:opening",
            ),
        )
    archived_accounts = tuple(
        policy.account_id
        for policy in ALL_RESEARCH_POLICIES
        if policy.account_id != TARGET_POLICY.account_id
    )
    conn.execute(
        "UPDATE paper_accounts SET status='ARCHIVED' "
        f"WHERE account_id IN ({','.join('?' for _ in archived_accounts)}) "
        "AND status!='ARCHIVED'",
        archived_accounts,
    )


def _enforce_active_account_set(conn: sqlite3.Connection) -> None:
    """Archive every account outside the two deliberate post-cutover ledgers."""

    conn.execute(
        "UPDATE paper_accounts SET status='ARCHIVED' "
        "WHERE status='ACTIVE' AND account_id NOT IN (?, ?)",
        (LIVE_STABILITY_ACCOUNT_ID, TARGET_POLICY.account_id),
    )


def _add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    existing: set[str],
    columns: dict[str, str],
) -> None:
    """Additive migration safe when multiple services initialize together."""

    for column, column_type in columns.items():
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        except sqlite3.OperationalError as exc:
            current = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in current:
                raise exc
        existing.add(column)


def init_store(self) -> None:
    with _exclusive_init_lock(getattr(self, "db_path", None)):
        _init_store_locked(self)


def _init_store_locked(self) -> None:
    with self.connect() as conn:
        decision_table_existed = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='decision_snapshots'"
        ).fetchone() is not None
        conn.executescript(SCHEMA)
        verification_columns = {
            row[1]: row
            for row in conn.execute(
                "PRAGMA table_info(paper_settlement_verifications)"
            ).fetchall()
        }
        if verification_columns.get("final_high_f", (None,) * 4)[3]:
            conn.execute(
                "ALTER TABLE paper_settlement_verifications "
                "RENAME TO paper_settlement_verifications_legacy"
            )
            conn.execute(
                """
                CREATE TABLE paper_settlement_verifications (
                    order_id INTEGER PRIMARY KEY,
                    checked_at TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    booked_high_f REAL NOT NULL,
                    final_high_f REAL,
                    verification_status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO paper_settlement_verifications SELECT * "
                "FROM paper_settlement_verifications_legacy"
            )
            conn.execute("DROP TABLE paper_settlement_verifications_legacy")
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()
        }
        _add_missing_columns(conn, "paper_orders", existing, PAPER_ORDER_AUDIT_COLUMNS)
        # Legacy rows have no trustworthy partial queue state. Preserve their
        # booked quantity and mark the evidence generation explicitly; current
        # orders write all progress fields at insertion time.
        conn.execute(
            """
            UPDATE paper_orders
            SET requested_contracts = COALESCE(requested_contracts, contracts),
                filled_contracts = COALESCE(
                    filled_contracts,
                    CASE
                        WHEN status IN ('PAPER_FILLED', 'PAPER_CLOSED', 'PAPER_SETTLED')
                        THEN contracts
                        ELSE 0
                    END
                ),
                remaining_contracts = COALESCE(
                    remaining_contracts,
                    CASE WHEN status = 'PAPER_LIMIT_RESTING' THEN contracts ELSE 0 END
                ),
                queue_remaining = COALESCE(
                    queue_remaining,
                    CASE
                        WHEN status = 'PAPER_LIMIT_RESTING'
                        THEN MAX(0, COALESCE(entry_bid_size, 0))
                        ELSE 0
                    END
                ),
                execution_model_version = COALESCE(
                    execution_model_version, 'legacy-pre-exec-v3'
                )
            WHERE requested_contracts IS NULL
               OR filled_contracts IS NULL
               OR remaining_contracts IS NULL
               OR queue_remaining IS NULL
               OR execution_model_version IS NULL
            """
        )
        existing_trade = {
            row[1]
            for row in conn.execute("PRAGMA table_info(dataset_kalshi_trades)").fetchall()
        }
        _add_missing_columns(
            conn,
            "dataset_kalshi_trades",
            existing_trade,
            {
                "taker_book_side": "TEXT",
                "maker_side": "TEXT",
                "last_seen_at": "TEXT",
            },
        )
        existing_probability = {
            row[1]
            for row in conn.execute("PRAGMA table_info(probability_snapshots)").fetchall()
        }
        _add_missing_columns(
            conn, "probability_snapshots", existing_probability, PROBABILITY_AUDIT_COLUMNS
        )
        existing_decision = {
            row[1]
            for row in conn.execute("PRAGMA table_info(decision_snapshots)").fetchall()
        }
        _add_missing_columns(
            conn, "decision_snapshots", existing_decision, DECISION_AUDIT_COLUMNS
        )
        existing_daily_goals = {
            row[1]
            for row in conn.execute("PRAGMA table_info(research_daily_goals)").fetchall()
        }
        _add_missing_columns(
            conn,
            "research_daily_goals",
            existing_daily_goals,
            RESEARCH_DAILY_GOAL_AUDIT_COLUMNS,
        )
        _backfill_legacy_research_daily_goal_fingerprints(conn)
        existing_context = {
            row[1]
            for row in conn.execute("PRAGMA table_info(scan_context_snapshots)").fetchall()
        }
        _add_missing_columns(
            conn,
            "scan_context_snapshots",
            existing_context,
            SCAN_CONTEXT_AUDIT_COLUMNS,
        )
        existing_shadow = {
            row[1]
            for row in conn.execute("PRAGMA table_info(research_shadow_orders)").fetchall()
        }
        _add_missing_columns(
            conn, "research_shadow_orders", existing_shadow, RESEARCH_SHADOW_AUDIT_COLUMNS
        )
        for table in ("paper_monitor_snapshots", "research_shadow_monitor_snapshots"):
            existing_monitor = {
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            _add_missing_columns(conn, table, existing_monitor, MONITOR_AUDIT_COLUMNS)
        _ensure_research_identity_triggers(conn)
        _ensure_research_daily_goal_triggers(conn)
        _ensure_research_experiment_triggers(conn)
        existing_forecast = {
            row[1]
            for row in conn.execute("PRAGMA table_info(forecast_snapshots)").fetchall()
        }
        _add_missing_columns(
            conn,
            "forecast_snapshots",
            existing_forecast,
            {"station_id": "TEXT DEFAULT 'KSFO'"},
        )
        _migrate_legacy_profile_names(conn)
        _migrate_closed_row_position_won(conn)
        scan_context_index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_decision_snapshots_scan_context'"
        ).fetchone()
        if (
            scan_context_index is not None
            and "WHERE SCAN_CONTEXT_ID IS NOT NULL"
            not in str(scan_context_index[0] or "").upper()
        ):
            conn.execute("DROP INDEX IF EXISTS idx_decision_snapshots_scan_context")
        conn.executescript(INDEXES)
        decision_table_has_rows = (
            conn.execute("SELECT 1 FROM decision_snapshots LIMIT 1").fetchone()
            is not None
        )
        if not decision_table_existed or not decision_table_has_rows:
            conn.execute(DECISION_SNAPSHOT_REPORT_INDEX)
            conn.execute(DECISION_SNAPSHOT_SAMPLE_INDEX)
            conn.execute(DECISION_SNAPSHOT_PENDING_RESEARCH_INDEX)
            conn.executescript(DECISION_SNAPSHOT_RETENTION_INDEXES)
        elif (
            conn.execute(
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
        if (
            decision_table_existed
            and decision_table_has_rows
            and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_decision_snapshots_pending_research_admission",),
            ).fetchone()
            is None
        ):
            logger.warning(
                "decision_snapshots is nonempty but the pending research "
                "admission index is missing; pause paper scan/monitor and run "
                "deploy/aws/create_decision_snapshot_index.sh"
            )
        if (
            decision_table_existed
            and decision_table_has_rows
            and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_decision_snapshots_retention_dedup",),
            ).fetchone()
            is None
        ):
            logger.warning(
                "decision_snapshots is nonempty but the retention indexes are "
                "missing; the nightly prune will fall back to full scans and "
                "can exceed its systemd start timeout. Pause paper "
                "scan/monitor and run deploy/aws/create_retention_indexes.sh"
            )
        self._expire_pre_current_execution_orders(conn)
        self._ensure_shared_paper_account(conn)
        self._ensure_live_stability_account(conn)
        self._ensure_open_position_guard_index(conn)
        _ensure_research_sleeve_accounts(conn)
        _enforce_active_account_set(conn)


def ensure_open_position_guard_index(self, conn: sqlite3.Connection) -> None:
    """Migrate the active-order guard to account scope, or fail closed.

    Duplicate validation happens before the prior profile-scoped index is
    dropped. A dirty legacy book therefore keeps its existing protection and
    cannot enable target/motion writes without the required account boundary.
    """
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        f"AND name='{_OPEN_POSITION_GUARD_NAME}'"
    ).fetchone()
    existing_sql = str(existing[0] or "") if existing else ""
    is_current = bool(
        existing
        and _open_position_guard_is_canonical(conn, existing_sql)
    )
    if is_current:
        return

    offending = conn.execute(
        """
        SELECT COALESCE(account_id, 'paper-shared') AS account_id,
               target_date,
               market_ticker,
               UPPER(COALESCE(side, 'YES')) AS side,
               COUNT(*) AS open_orders
        FROM paper_orders
        WHERE status IN (
            'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
            'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
        )
          AND settled_at IS NULL
          AND closed_at IS NULL
        GROUP BY 1, 2, 3, 4
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if offending:
        groups = "; ".join(
            f"{row[4]}x {row[2]} {row[3]} [{row[0]}] on {row[1]}"
            for row in offending
        )
        raise sqlite3.IntegrityError(
            "account-scoped open-position guard migration blocked by "
            f"{len(offending)} duplicate active group(s): {groups}"
        )

    if existing:
        conn.execute(f"DROP INDEX {_OPEN_POSITION_GUARD_NAME}")
    try:
        conn.execute(OPEN_POSITION_GUARD_INDEX)
    except sqlite3.IntegrityError as exc:  # concurrent writes outside init lock
        raise sqlite3.IntegrityError(
            "account-scoped open-position guard could not be built"
        ) from exc


def _decoded_position_won(side: object, resolved_yes: object) -> bool:
    """Decode a stored ``resolved_yes`` exactly the way every reader decodes it.

    The readers are Python -- ``db.settle_paper_orders``,
    ``backtest_rescore``, and ``research_shadow`` all evaluate
    ``resolved_yes if side == "YES" else not resolved_yes`` -- so the truth test
    is Python's ``bool`` and the side default is ``str(side or "YES")``.

    SQL is deliberately not used for this. SQLite's truthiness and Python's
    disagree off the ``{0, 1}`` domain: ``1 - resolved_yes`` on a stored ``2``
    yields ``-1``, which is truthy where the reader said False, and on the text
    ``'bad'`` yields ``1``, again truthy where the reader said False; a plain
    ``CASE WHEN resolved_yes`` does not fix it either, because SQLite reads
    ``'bad'`` as 0 while ``bool('bad')`` is True. Empty-string sides differ too:
    ``str('' or "YES")`` is ``YES`` while ``COALESCE(side, 'YES')`` leaves
    ``''``. Out-of-domain values are not hypothetical in this project --
    ``restatement`` raises ``RESOLVED_YES_INVALID`` for them and the exec-v4
    replay tests parameterize ``2`` and ``'bad'`` -- so decoding here is what
    makes the migration's "no row's win/loss can move" claim actually true.
    """

    resolves_yes = bool(resolved_yes)
    if str(side or "YES").upper() == "YES":
        return resolves_yes
    return not resolves_yes


_CLOSED_POSITION_WON_MIGRATION_KEY = "closed_row_position_won_v1"


def _migrate_closed_row_position_won(conn: sqlite3.Connection) -> int:
    """Stop `resolved_yes` from meaning "this position made money" on closes.

    ``close_paper_order`` used to derive ``resolved_yes`` from
    ``sign(realized_pnl)`` and the side, so on a ``PAPER_CLOSED`` row the column
    named after the *market's* outcome actually recorded the *position's* P&L
    sign. On production those two things disagree on roughly 40% of the closed
    rows whose true market outcome is obtainable, and 45 ticker-days carry both
    labels across their own lots.

    The transform is lossless. The stored value carries no information beyond
    the P&L sign, and the P&L is already in ``realized_pnl``; every reader
    decoded it back to a win/loss flag, so this migration writes exactly that
    decoded flag into the new explicit ``position_won`` column and clears
    ``resolved_yes`` back to NULL. Win/loss and hit-rate are unchanged by
    construction. The same key is removed from the persisted
    ``outcome_diagnostics_json`` outcome block, which the payload builder now
    omits on close as well; ``position_won`` and ``win_loss_reason`` were
    already correct there and are left untouched.
    """

    if conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_key=?",
        (_CLOSED_POSITION_WON_MIGRATION_KEY,),
    ).fetchone() is not None:
        return 0
    rows = conn.execute(
        "SELECT id, side, resolved_yes, outcome_diagnostics_json FROM paper_orders "
        "WHERE status = 'PAPER_CLOSED' AND resolved_yes IS NOT NULL"
    ).fetchall()
    migrated = 0
    for order_id, side, resolved_yes, outcome_json in rows:
        # Decode with the same rule every reader used, so no row's win/loss can
        # move even if a stored value were inconsistent with its P&L sign.
        position_won = _decoded_position_won(side, resolved_yes)
        cleaned_json = outcome_json
        if outcome_json:
            try:
                payload = json.loads(outcome_json)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("outcome"), dict):
                if payload["outcome"].pop("resolved_yes", None) is not None:
                    cleaned_json = json.dumps(payload, sort_keys=True)
        conn.execute(
            "UPDATE paper_orders SET position_won = ?, resolved_yes = NULL, "
            "outcome_diagnostics_json = ? WHERE id = ?",
            (1 if position_won else 0, cleaned_json, order_id),
        )
        migrated += 1
    # Settled rows keep resolved_yes (a real market outcome) and gain the
    # explicit position flag derived from it, so both columns mean one thing.
    # Decoded through the same helper as the closed rows above: the set-based
    # form this replaced agreed with the readers only on the {0, 1} domain, so
    # a single out-of-domain stored value would have flipped a win into a loss.
    settled_rows = conn.execute(
        "SELECT id, side, resolved_yes FROM paper_orders "
        "WHERE status = 'PAPER_SETTLED' AND resolved_yes IS NOT NULL "
        "AND position_won IS NULL"
    ).fetchall()
    if settled_rows:
        conn.executemany(
            "UPDATE paper_orders SET position_won = ? WHERE id = ?",
            [
                (1 if _decoded_position_won(side, resolved_yes) else 0, order_id)
                for order_id, side, resolved_yes in settled_rows
            ],
        )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (migration_key, completed_at) VALUES (?, ?)",
        (_CLOSED_POSITION_WON_MIGRATION_KEY, _now()),
    )
    return migrated


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
