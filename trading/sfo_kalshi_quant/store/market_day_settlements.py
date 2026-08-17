"""Settlement outcome for every market-day the paper book actually traded.

Why this table exists
---------------------
``paper_orders.settlement_high_f`` / ``paper_orders.resolved_yes`` are written
by exactly one code path: :meth:`PaperStore.settle_paper_orders`.  That path
only touches rows that are *still open* when the day settles.  A market-day
whose every lot was exited early by the monitor therefore leaves **no record of
what the market did** -- the journal keeps the exit price and the realized P&L
and nothing else.  Those days are structurally invisible, and they are exactly
the population an exit-rule change would have to be judged on, so no exit rule
can currently be proven better or worse than another.

This module records the final outcome for **every** traded market-day,
regardless of whether a position survived to settlement, in its own table.  It
deliberately does not touch ``paper_orders``: that table feeds the policy
fingerprint, replay, and restatement machinery, and adding or rewriting a column
there would put the real-money readiness clock at risk.  A new table changes no
existing row, gate, or policy, so recording here is evidence-cost-zero.

Truth sources
-------------
Only sources that were independently validated are accepted, in this order of
authority:

``settlement_path`` (rank 3)
    The integer °F high handed to :meth:`PaperStore.settle_paper_orders` at the
    moment the day settled.  Same number the ledger booked against.

``settled_sibling`` (rank 2)
    ``settlement_high_f`` persisted on a ``PAPER_SETTLED`` order for the same
    ``(series_ticker, target_date)``.  Verified to have zero internal conflicts.

``dataset_kalshi_markets`` (rank 1)
    The exchange's own finalized ``result`` for the exact ticker.  Verified to
    have zero disagreements with the settled-sibling highs.  Carries no
    temperature, so ``settlement_high_f`` stays NULL on rows sourced this way.

Sources that were tested and **rejected** -- do not reintroduce them:
``probability_snapshots.observed_high_f`` (a running intraday max, exact on only
1.5% of days), station METAR daily max (27.4% exact and systematically 1°F low),
and ``market_snapshots.result`` (only ever the string ``active``).

Unrecoverable history
---------------------
A traded market-day is **unrecoverable** when neither surviving source covers
it: no ``PAPER_SETTLED`` order exists for its ``(series_ticker, target_date)``
and ``dataset_kalshi_markets`` holds no finalized ``yes``/``no`` result for its
ticker.  Those days predate any durable capture of the outcome and cannot be
reconstructed from this database; :func:`backfill_market_day_settlements`
reports them explicitly rather than guessing.  Every future day is covered,
because the live settlement path now records the whole traded market-day.

Read-only by contract
---------------------
Nothing in the trading path may read this table.  It exists to measure
decisions after the fact, never to make one.  ``test_market_day_settlements.py``
enforces that with an allowlist over the package source.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from ..cities import city_for_market_ticker
from ..settlement_truth import integer_settlement_high_f, row_resolves_yes

MARKET_DAY_SETTLEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_day_settlements (
    market_ticker TEXT NOT NULL,
    target_date TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    settlement_high_f REAL,
    resolved_yes INTEGER NOT NULL,
    truth_source TEXT NOT NULL,
    truth_rank INTEGER NOT NULL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    traded_lots INTEGER NOT NULL DEFAULT 0,
    settled_lots INTEGER NOT NULL DEFAULT 0,
    closed_lots INTEGER NOT NULL DEFAULT 0,
    realized_pnl REAL,
    PRIMARY KEY (market_ticker, target_date)
);
"""

MARKET_DAY_SETTLEMENT_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_market_day_settlements_target
ON market_day_settlements (target_date, series_ticker);
"""

TRUTH_SOURCE_SETTLEMENT_PATH = "settlement_path"
TRUTH_SOURCE_SETTLED_SIBLING = "settled_sibling"
TRUTH_SOURCE_DATASET_MARKET = "dataset_kalshi_markets"

TRUTH_SOURCE_RANKS: dict[str, int] = {
    TRUTH_SOURCE_SETTLEMENT_PATH: 3,
    TRUTH_SOURCE_SETTLED_SIBLING: 2,
    TRUTH_SOURCE_DATASET_MARKET: 1,
}

# A market-day counts as traded once the book actually held the position. A
# quote that rested and expired without ever filling took no market risk, so it
# is not part of the exit-rule population this table exists to measure.
TRADED_STATUSES: tuple[str, ...] = (
    "PAPER_FILLED",
    "PAPER_PARTIALLY_FILLED",
    "PAPER_PARTIAL_EXPIRED",
    "PAPER_CLOSED",
    "PAPER_SETTLED",
)

_TRADED_PLACEHOLDERS = ", ".join("?" for _ in TRADED_STATUSES)

_TRADED_MARKET_DAY_SQL = f"""
SELECT
    market_ticker,
    target_date,
    MAX(strike_type) AS strike_type,
    MAX(floor_strike) AS floor_strike,
    MAX(cap_strike) AS cap_strike,
    MAX(label) AS label,
    COUNT(*) AS traded_lots,
    SUM(CASE WHEN status = 'PAPER_SETTLED' THEN 1 ELSE 0 END) AS settled_lots,
    SUM(CASE WHEN status = 'PAPER_CLOSED' THEN 1 ELSE 0 END) AS closed_lots,
    SUM(COALESCE(realized_pnl, 0.0)) AS realized_pnl
FROM paper_orders
WHERE status IN ({_TRADED_PLACEHOLDERS})
"""

_UPSERT_SQL = """
INSERT INTO market_day_settlements (
    market_ticker, target_date, series_ticker, recorded_at,
    settlement_high_f, resolved_yes, truth_source, truth_rank,
    strike_type, floor_strike, cap_strike,
    traded_lots, settled_lots, closed_lots, realized_pnl
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(market_ticker, target_date) DO UPDATE SET
    -- Derived counters always refresh: they are a projection of paper_orders,
    -- never evidence of their own.
    recorded_at = excluded.recorded_at,
    series_ticker = excluded.series_ticker,
    strike_type = excluded.strike_type,
    floor_strike = excluded.floor_strike,
    cap_strike = excluded.cap_strike,
    traded_lots = excluded.traded_lots,
    settled_lots = excluded.settled_lots,
    closed_lots = excluded.closed_lots,
    realized_pnl = excluded.realized_pnl,
    -- Outcome fields only move to an equal-or-better authority, so a late
    -- low-authority backfill can never downgrade a recorded settlement.
    settlement_high_f = CASE
        WHEN excluded.truth_rank > market_day_settlements.truth_rank
             OR (excluded.truth_rank = market_day_settlements.truth_rank
                 AND excluded.settlement_high_f IS NOT NULL)
        THEN excluded.settlement_high_f
        ELSE market_day_settlements.settlement_high_f
    END,
    resolved_yes = CASE
        WHEN excluded.truth_rank >= market_day_settlements.truth_rank
        THEN excluded.resolved_yes
        ELSE market_day_settlements.resolved_yes
    END,
    truth_source = CASE
        WHEN excluded.truth_rank >= market_day_settlements.truth_rank
        THEN excluded.truth_source
        ELSE market_day_settlements.truth_source
    END,
    truth_rank = MAX(excluded.truth_rank, market_day_settlements.truth_rank)
"""


def _series_ticker_for(market_ticker: str) -> str | None:
    city = city_for_market_ticker(str(market_ticker))
    return city.series_ticker if city is not None else None


def _traded_market_days(
    conn: sqlite3.Connection,
    *,
    series_ticker: str | None = None,
    target_date: str | None = None,
) -> list[sqlite3.Row]:
    query = _TRADED_MARKET_DAY_SQL
    params: list[Any] = list(TRADED_STATUSES)
    if target_date is not None:
        query += " AND target_date = ?"
        params.append(target_date)
    if series_ticker:
        query += " AND market_ticker LIKE ?"
        params.append(f"{series_ticker}-%")
    query += " GROUP BY market_ticker, target_date ORDER BY target_date, market_ticker"
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.row_factory = previous_factory


def _existing_outcomes(
    conn: sqlite3.Connection, keys: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], tuple[int, int]]:
    """Map ``(ticker, target_date)`` to its recorded ``(resolved_yes, rank)``."""

    recorded: dict[tuple[str, str], tuple[int, int]] = {}
    for ticker, target in keys:
        row = conn.execute(
            "SELECT resolved_yes, truth_rank FROM market_day_settlements "
            "WHERE market_ticker = ? AND target_date = ?",
            (ticker, target),
        ).fetchone()
        if row is not None:
            recorded[(ticker, target)] = (int(row[0]), int(row[1]))
    return recorded


def record_market_day_settlements(
    conn: sqlite3.Connection,
    *,
    target_date: str,
    settlement_high_f: float,
    recorded_at: str,
    series_ticker: str | None = None,
    truth_source: str = TRUTH_SOURCE_SETTLEMENT_PATH,
) -> dict[str, Any]:
    """Record the outcome of every market-day traded on ``target_date``.

    Idempotent: re-running with the same truth rewrites the same values.  Safe
    to call inside the settlement transaction -- it only writes
    ``market_day_settlements`` and reads ``paper_orders``.
    """

    if truth_source not in TRUTH_SOURCE_RANKS:
        raise ValueError(f"unknown settlement truth source {truth_source!r}")
    rank = TRUTH_SOURCE_RANKS[truth_source]
    high = integer_settlement_high_f(settlement_high_f)
    rows = _traded_market_days(
        conn, series_ticker=series_ticker, target_date=target_date
    )
    existing = _existing_outcomes(
        conn, ((str(row["market_ticker"]), str(row["target_date"])) for row in rows)
    )
    written = 0
    skipped_unknown_series = 0
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row["market_ticker"])
        series = _series_ticker_for(ticker)
        if series is None:
            # A ticker outside the configured city registry has no settlement
            # authority here; recording it would invent one.
            skipped_unknown_series += 1
            continue
        resolved_yes = row_resolves_yes(row, high)
        prior = existing.get((ticker, str(row["target_date"])))
        if prior is not None and prior[0] != int(resolved_yes) and rank >= prior[1]:
            conflicts.append(
                {
                    "market_ticker": ticker,
                    "target_date": str(row["target_date"]),
                    "recorded_resolved_yes": prior[0],
                    "incoming_resolved_yes": int(resolved_yes),
                    "incoming_truth_source": truth_source,
                }
            )
        conn.execute(
            _UPSERT_SQL,
            (
                ticker,
                str(row["target_date"]),
                series,
                recorded_at,
                high,
                1 if resolved_yes else 0,
                truth_source,
                rank,
                row["strike_type"],
                row["floor_strike"],
                row["cap_strike"],
                int(row["traded_lots"] or 0),
                int(row["settled_lots"] or 0),
                int(row["closed_lots"] or 0),
                float(row["realized_pnl"] or 0.0),
            ),
        )
        written += 1
    return {
        "target_date": target_date,
        "series_ticker": series_ticker,
        "settlement_high_f": high,
        "truth_source": truth_source,
        "market_days_recorded": written,
        "skipped_unknown_series": skipped_unknown_series,
        "conflicts": conflicts,
    }


def unrecorded_traded_target_dates(
    conn: sqlite3.Connection, *, series_ticker: str | None = None
) -> list[str]:
    """Traded target dates that still have at least one unrecorded market-day.

    This is the residual of the live path: ``settle_paper_orders`` covers a
    whole ``(series, target_date)`` the moment *anything* on it settles, but a
    day where every single lot exited early never reaches that path at all.
    """

    rows = _traded_market_days(conn, series_ticker=series_ticker)
    recorded = _existing_outcomes(
        conn, ((str(row["market_ticker"]), str(row["target_date"])) for row in rows)
    )
    missing = {
        str(row["target_date"])
        for row in rows
        if (str(row["market_ticker"]), str(row["target_date"])) not in recorded
    }
    return sorted(missing)


def _settled_sibling_highs(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Highest-authority surviving truth: what the ledger actually booked."""

    highs: dict[tuple[str, str], float] = {}
    rows = conn.execute(
        "SELECT market_ticker, target_date, settlement_high_f FROM paper_orders "
        "WHERE status = 'PAPER_SETTLED' AND settlement_high_f IS NOT NULL"
    ).fetchall()
    for market_ticker, target_date, high in rows:
        series = _series_ticker_for(str(market_ticker))
        if series is None:
            continue
        highs.setdefault((series, str(target_date)), float(high))
    return highs


def _dataset_market_results(conn: sqlite3.Connection) -> dict[str, bool]:
    """Exchange-finalized YES/NO per ticker, when the dataset table is present."""

    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'dataset_kalshi_markets'"
    ).fetchone()
    if present is None:
        return {}
    rows = conn.execute(
        "SELECT ticker, result FROM dataset_kalshi_markets "
        "WHERE market_status = 'finalized' AND LOWER(result) IN ('yes', 'no')"
    ).fetchall()
    return {str(ticker): str(result).lower() == "yes" for ticker, result in rows}


def backfill_market_day_settlements(
    conn: sqlite3.Connection,
    *,
    recorded_at: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Populate historical market-days from the two validated truth sources.

    Idempotent and additive.  Market-days already recorded by a stronger source
    are left alone.  Days that neither source covers are returned under
    ``unrecoverable`` rather than being filled with a guess.
    """

    rows = _traded_market_days(conn)
    existing = _existing_outcomes(
        conn, ((str(row["market_ticker"]), str(row["target_date"])) for row in rows)
    )
    sibling_highs = _settled_sibling_highs(conn)
    dataset_results = _dataset_market_results(conn)

    from_sibling = 0
    from_dataset = 0
    already_recorded = 0
    unrecoverable: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row["market_ticker"])
        target = str(row["target_date"])
        series = _series_ticker_for(ticker)
        if series is None:
            unrecoverable.append(
                {
                    "market_ticker": ticker,
                    "target_date": target,
                    "reason": "ticker is outside the configured city registry",
                }
            )
            continue
        high = sibling_highs.get((series, target))
        dataset_resolved = dataset_results.get(ticker)
        if high is not None:
            truth_source = TRUTH_SOURCE_SETTLED_SIBLING
            resolved_yes = row_resolves_yes(row, integer_settlement_high_f(high))
        elif dataset_resolved is not None:
            truth_source = TRUTH_SOURCE_DATASET_MARKET
            resolved_yes = dataset_resolved
        else:
            unrecoverable.append(
                {
                    "market_ticker": ticker,
                    "target_date": target,
                    "reason": (
                        "no settled sibling for this series-day and no finalized "
                        "dataset_kalshi_markets result for this ticker"
                    ),
                }
            )
            continue
        rank = TRUTH_SOURCE_RANKS[truth_source]
        prior = existing.get((ticker, target))
        if prior is not None and prior[1] >= rank:
            already_recorded += 1
            continue
        if truth_source == TRUTH_SOURCE_SETTLED_SIBLING:
            from_sibling += 1
        else:
            from_dataset += 1
        if dry_run:
            continue
        conn.execute(
            _UPSERT_SQL,
            (
                ticker,
                target,
                series,
                recorded_at,
                integer_settlement_high_f(high) if high is not None else None,
                1 if resolved_yes else 0,
                truth_source,
                rank,
                row["strike_type"],
                row["floor_strike"],
                row["cap_strike"],
                int(row["traded_lots"] or 0),
                int(row["settled_lots"] or 0),
                int(row["closed_lots"] or 0),
                float(row["realized_pnl"] or 0.0),
            ),
        )
    return {
        "traded_market_days": len(rows),
        "already_recorded": already_recorded,
        "recorded_from_settled_sibling": from_sibling,
        "recorded_from_dataset_markets": from_dataset,
        "unrecoverable": unrecoverable,
        "dry_run": dry_run,
    }
