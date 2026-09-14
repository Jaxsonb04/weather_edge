"""Reconcile every booked paper settlement against the exchange's own result.

Why this exists
---------------
``verify_paper_settlements`` compares the high a lot was booked against with the
final NWS CLI maximum in ``weather.db``.  On the unattended settle path that is
the *same* number the lot was just settled from, so it can only notice a CLI
value that changed after settlement.  It cannot notice the exchange resolving
the market on a different number than the CLI archive holds.

That case is real.  Kalshi moved daily-high settlement to The Weather Company on
2026-08-14/15.  Its ``expiration_value`` has equalled the NWS CLI integer on 434
of 435 station-days since, and all 53 auditable settled lots in this book agree
with the exchange -- the scorer is not wrong.  The exception was Miami
2026-08-29: the NWS issued two conflicting CLI versions (maximum 90 at 04:24
EDT, then 85 at 05:10 EDT) and the exchange settled on 90.
``forecaster/clisfo.py`` keeps the newest final version it sees, so a lot booked
from the archive would have settled on 85 -- paying out the wrong side of every
bin between the two numbers -- and the booked-vs-CLI verification would have
called it ``MATCH``.

What it records
---------------
One row per settled lot in ``paper_settlement_exchange_checks``:

* ``booked_high_f`` and ``booked_winner`` -- the integer high the journal settled
  on and the market side (``YES``/``NO``) it therefore paid out;
* ``kalshi_status``, ``kalshi_result``, ``kalshi_expiration_value`` and
  ``kalshi_source_endpoint`` -- what the exchange's public market endpoint says;
* ``verification_status``, one of

  ``MATCH``
      finalized, and both the result and the settlement value agree;
  ``MISMATCH``
      finalized, and the result or the settlement value disagrees --
      ``mismatch_reason`` names which (``result``, ``expiration_value``,
      ``result_not_yes_no``);
  ``KALSHI_PENDING``
      the exchange has not finalized the market yet;
  ``UNCHECKED``
      the market could not be fetched or read (``check_error`` says why).  Never
      read as agreement; retried on a later run.

The result and the settlement value are compared separately on purpose.  A lot
on a bin both numbers fall on the same side of (booked 85, exchange 90, bin
"94-95") paid out correctly, while a sibling lot between the two numbers did
not.  Reporting only payout disagreements would hide the one signal that says
the day's truth source diverged from the exchange.

Finalized exchange results are cached per ticker in
``kalshi_market_resolutions``: a market is fetched until it finalizes, then
never again.  Pending markets are never cached.

What it deliberately does not record
------------------------------------
Which CLI *issuance* the booked high came from.  ``cli_settlements`` holds
``station_id, local_date, max_temperature_f, fetched_at, source, is_final``, and
``forecaster/clisfo.CliReport`` parses a report date, a maximum and a
preliminary flag -- nothing parses or persists the product's issuance time or
version.  ``fetched_at`` is when this project fetched the product, not when the
NWS issued it, so recording it as an issuance would invent provenance.

Non-blocking and read-only by contract
--------------------------------------
The check runs after settlement and never feeds back into it.  It writes only
these two tables -- never ``paper_orders``, the ledger, or
``paper_settlement_verifications``, which ``restatement.py`` classifies evidence
from -- so it moves no gate, fingerprint, or version.  A ``MISMATCH`` is an
incident signal to open a restatement, not an instruction to edit the journal.
Nothing in the trading path may read these tables;
``test_exchange_settlement_checks.py`` enforces that with an allowlist.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from .._util import _optional_float, _row_value
from ..settlement_truth import integer_settlement_high_f, row_resolves_yes

EXCHANGE_CHECK_MATCH = "MATCH"
EXCHANGE_CHECK_MISMATCH = "MISMATCH"
EXCHANGE_CHECK_PENDING = "KALSHI_PENDING"
EXCHANGE_CHECK_UNCHECKED = "UNCHECKED"
EXCHANGE_CHECK_VERDICTS: tuple[str, ...] = (
    EXCHANGE_CHECK_MATCH,
    EXCHANGE_CHECK_MISMATCH,
    EXCHANGE_CHECK_PENDING,
    EXCHANGE_CHECK_UNCHECKED,
)
# Verdicts that need a finalized market. Once a lot holds one, a later pending
# or unreadable observation can never replace it.
DECIDED_EXCHANGE_VERDICTS: tuple[str, ...] = (
    EXCHANGE_CHECK_MATCH,
    EXCHANGE_CHECK_MISMATCH,
)

MISMATCH_REASON_RESULT = "result"
MISMATCH_REASON_EXPIRATION_VALUE = "expiration_value"
MISMATCH_REASON_RESULT_NOT_YES_NO = "result_not_yes_no"

EXCHANGE_FINALIZED_STATUS = "finalized"
_BINARY_RESULTS = ("yes", "no")
_SQL_IN_CHUNK = 500

EXCHANGE_SETTLEMENT_CHECK_SCHEMA = """
CREATE TABLE IF NOT EXISTS kalshi_market_resolutions (
    market_ticker TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    market_status TEXT NOT NULL,
    result TEXT NOT NULL,
    expiration_value REAL
);

CREATE TABLE IF NOT EXISTS paper_settlement_exchange_checks (
    order_id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    target_date TEXT NOT NULL,
    booked_high_f REAL NOT NULL,
    booked_winner TEXT NOT NULL,
    kalshi_status TEXT,
    kalshi_result TEXT,
    kalshi_expiration_value REAL,
    kalshi_source_endpoint TEXT,
    verification_status TEXT NOT NULL,
    mismatch_reason TEXT,
    check_error TEXT
);
"""

EXCHANGE_SETTLEMENT_CHECK_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_paper_settlement_exchange_checks_status
    ON paper_settlement_exchange_checks (verification_status, target_date);
"""

_DECIDED_SQL = ", ".join(f"'{verdict}'" for verdict in DECIDED_EXCHANGE_VERDICTS)

_LOT_SELECT = """
SELECT o.id AS id,
       o.market_ticker AS market_ticker,
       o.target_date AS target_date,
       o.settlement_high_f AS settlement_high_f,
       o.resolved_yes AS resolved_yes,
       o.strike_type AS strike_type,
       o.floor_strike AS floor_strike,
       o.cap_strike AS cap_strike,
       o.label AS label
FROM paper_orders AS o
LEFT JOIN paper_settlement_exchange_checks AS c ON c.order_id = o.id
WHERE o.status = 'PAPER_SETTLED'
  AND o.settled_at IS NOT NULL
  AND o.settlement_high_f IS NOT NULL
"""

_CHECK_COLUMNS: tuple[str, ...] = (
    "order_id",
    "checked_at",
    "market_ticker",
    "target_date",
    "booked_high_f",
    "booked_winner",
    "kalshi_status",
    "kalshi_result",
    "kalshi_expiration_value",
    "kalshi_source_endpoint",
    "verification_status",
    "mismatch_reason",
    "check_error",
)
_CHECK_INSERT_COLUMNS = ", ".join(_CHECK_COLUMNS)
_CHECK_PLACEHOLDERS = ", ".join("?" for _ in _CHECK_COLUMNS)
_CHECK_ASSIGNMENTS = ", ".join(
    f"{column} = excluded.{column}" for column in _CHECK_COLUMNS if column != "order_id"
)
# The WHERE clause is the no-downgrade rule: a decided verdict is only ever
# replaced by another decided verdict.
_CHECK_UPSERT_SQL = (
    f"INSERT INTO paper_settlement_exchange_checks ({_CHECK_INSERT_COLUMNS}) "
    f"VALUES ({_CHECK_PLACEHOLDERS}) "
    f"ON CONFLICT(order_id) DO UPDATE SET {_CHECK_ASSIGNMENTS} "
    "WHERE paper_settlement_exchange_checks.verification_status "
    f"NOT IN ({_DECIDED_SQL}) "
    f"OR excluded.verification_status IN ({_DECIDED_SQL})"
)


def booked_winner_for_lot(row: object) -> str:
    """The market side (``YES``/``NO``) a settled lot's journal paid out on.

    ``settle_paper_orders`` writes ``resolved_yes`` on every lot it settles, and
    ``position_won`` -- what the ledger paid -- is derived from it.  A row
    without it is resolved from its booked high through the same
    :func:`row_resolves_yes` rule settlement uses.
    """

    resolved = _row_value(row, "resolved_yes")
    if resolved is None:
        booked_high = integer_settlement_high_f(_row_value(row, "settlement_high_f"))
        resolved = row_resolves_yes(row, booked_high)
    return "YES" if bool(resolved) else "NO"


def _unchecked_verdict(error: str | None) -> dict[str, Any]:
    return {
        "kalshi_status": None,
        "kalshi_result": None,
        "kalshi_expiration_value": None,
        "kalshi_source_endpoint": None,
        "verification_status": EXCHANGE_CHECK_UNCHECKED,
        "mismatch_reason": None,
        "check_error": error or "exchange market was not fetched",
    }


def classify_exchange_settlement(
    *,
    booked_high_f: float,
    booked_winner: str,
    resolution: Mapping[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Compare one booked lot with the exchange's view of its market."""

    if resolution is None:
        return _unchecked_verdict(error)
    status = str(resolution.get("market_status") or "").strip().lower()
    result = str(resolution.get("result") or "").strip().lower()
    value = _optional_float(resolution.get("expiration_value"))
    observed = {
        "kalshi_status": status or None,
        "kalshi_result": result or None,
        "kalshi_expiration_value": value,
        "kalshi_source_endpoint": resolution.get("source_endpoint"),
        "check_error": None,
    }
    if status != EXCHANGE_FINALIZED_STATUS:
        return {
            **observed,
            "verification_status": EXCHANGE_CHECK_PENDING,
            "mismatch_reason": None,
        }
    reasons: list[str] = []
    if result not in _BINARY_RESULTS:
        reasons.append(MISMATCH_REASON_RESULT_NOT_YES_NO)
    elif result.upper() != str(booked_winner).strip().upper():
        reasons.append(MISMATCH_REASON_RESULT)
    if value is not None and integer_settlement_high_f(value) != integer_settlement_high_f(
        booked_high_f
    ):
        reasons.append(MISMATCH_REASON_EXPIRATION_VALUE)
    return {
        **observed,
        "verification_status": EXCHANGE_CHECK_MISMATCH if reasons else EXCHANGE_CHECK_MATCH,
        "mismatch_reason": ",".join(reasons) if reasons else None,
    }


def settled_lots_for_exchange_check(
    conn: sqlite3.Connection,
    *,
    intervals: Mapping[str, tuple[str, str]] | None = None,
    settled_since: str | None = None,
    undecided_only: bool = False,
) -> list[sqlite3.Row]:
    """Settled lots to reconcile, bounded by target-date window or settle time.

    ``intervals`` is the ``{series_ticker: (first, last)}`` target-date window
    ``verify_paper_settlements`` uses; ``settled_since`` bounds by when the lot
    settled, so a lot whose target date is old but which settled just now (a
    late final CLI) is still reached.  ``undecided_only`` skips lots that
    already hold ``MATCH`` or ``MISMATCH``.
    """

    if intervals is None and settled_since is None:
        raise ValueError("bound the exchange check with intervals or settled_since")
    clauses: list[str] = []
    params: list[object] = []
    if intervals is not None:
        window: list[str] = []
        for series, (lower, upper) in intervals.items():
            window.append("(o.market_ticker LIKE ? AND o.target_date BETWEEN ? AND ?)")
            params.extend((f"{series}-%", lower, upper))
        if not window:
            return []
        clauses.append("(" + " OR ".join(window) + ")")
    if settled_since is not None:
        clauses.append("o.settled_at >= ?")
        params.append(settled_since)
    if undecided_only:
        clauses.append(
            f"(c.order_id IS NULL OR c.verification_status NOT IN ({_DECIDED_SQL}))"
        )
    sql = (
        _LOT_SELECT
        + "".join(f"  AND {clause}\n" for clause in clauses)
        + "ORDER BY o.target_date, o.id"
    )
    return conn.execute(sql, params).fetchall()


def cached_exchange_resolutions(
    conn: sqlite3.Connection, tickers: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Finalized exchange results already on record, keyed by ticker."""

    unique = sorted({str(ticker) for ticker in tickers})
    cached: dict[str, dict[str, Any]] = {}
    for start in range(0, len(unique), _SQL_IN_CHUNK):
        chunk = unique[start : start + _SQL_IN_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT market_ticker, fetched_at, source_endpoint, market_status, "
            "result, expiration_value FROM kalshi_market_resolutions "
            f"WHERE market_ticker IN ({placeholders})",
            chunk,
        ).fetchall()
        for ticker, fetched_at, endpoint, status, result, value in rows:
            cached[str(ticker)] = {
                "market_ticker": str(ticker),
                "fetched_at": fetched_at,
                "source_endpoint": endpoint,
                "market_status": status,
                "result": result,
                "expiration_value": value,
            }
    return cached


def record_exchange_resolution(
    conn: sqlite3.Connection,
    resolution: Mapping[str, Any],
    *,
    fetched_at: str,
) -> bool:
    """Cache a finalized exchange result; a pending market is never cached."""

    status = str(resolution.get("market_status") or "").strip().lower()
    if status != EXCHANGE_FINALIZED_STATUS:
        return False
    conn.execute(
        "INSERT INTO kalshi_market_resolutions "
        "(market_ticker, fetched_at, source_endpoint, market_status, result, "
        "expiration_value) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(market_ticker) DO NOTHING",
        (
            str(resolution["market_ticker"]),
            fetched_at,
            str(resolution.get("source_endpoint") or ""),
            status,
            str(resolution.get("result") or "").strip().lower(),
            _optional_float(resolution.get("expiration_value")),
        ),
    )
    return True


def record_exchange_settlement_checks(
    conn: sqlite3.Connection, checks: Iterable[Mapping[str, Any]]
) -> None:
    """Upsert one verdict per lot, never downgrading a decided verdict."""

    conn.executemany(
        _CHECK_UPSERT_SQL,
        [tuple(check[column] for column in _CHECK_COLUMNS) for check in checks],
    )


def standing_exchange_mismatches(conn: sqlite3.Connection) -> int:
    """Every ``MISMATCH`` on record, not just the ones found this run."""

    row = conn.execute(
        "SELECT COUNT(*) FROM paper_settlement_exchange_checks "
        "WHERE verification_status = ?",
        (EXCHANGE_CHECK_MISMATCH,),
    ).fetchone()
    return int(row[0])
