from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

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
    schema_version INTEGER NOT NULL DEFAULT 1
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
CREATE INDEX IF NOT EXISTS idx_market_snapshots_target
    ON market_snapshots (target_date, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_scan_context
    ON decision_snapshots (scan_context_id)
    WHERE scan_context_id IS NOT NULL;
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
    WHERE status IN (
        'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
        'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
    )
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
    "requested_contracts": "REAL",
    "filled_contracts": "REAL",
    "remaining_contracts": "REAL",
    "queue_remaining": "REAL",
    "execution_model_version": "TEXT",
    # Depth-aware partial closes (audit EX-02): the executed slice of a partial
    # close becomes its own PAPER_CLOSED row linked back to the original order.
    "parent_order_id": "INTEGER",
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
}

RESEARCH_SHADOW_AUDIT_COLUMNS = {
    "entry_decision_snapshot_id": "INTEGER",
    "diagnostics_json": "TEXT",
    "outcome_diagnostics_json": "TEXT",
}

MONITOR_AUDIT_COLUMNS = {
    "diagnostics_json": "TEXT",
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
}


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
        # booked quantity and mark the evidence generation explicitly; new v3
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
        self._expire_pre_v3_resting_orders(conn)
        self._ensure_shared_paper_account(conn)
        self._ensure_open_position_guard_index(conn)

def ensure_open_position_guard_index(self, conn: sqlite3.Connection) -> None:
    """Build the unique open-position backstop index, tolerating a dirty book.

    A database that still holds pre-existing duplicate OPEN orders (a book
    from before this guard, e.g. the 2026-06-18 incident) cannot build the
    unique index. Rather than brick every init()/scan, log which groups block
    it so an operator can close the surplus with `paper-close`; the index then
    builds automatically on the next run.
    """
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ux_paper_orders_open_market_side_profile'"
    ).fetchone()
    if existing and "PAPER_PARTIALLY_FILLED" not in str(existing[0] or ""):
        conn.execute("DROP INDEX ux_paper_orders_open_market_side_profile")
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
            WHERE status IN (
                'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
                'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
            )
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
