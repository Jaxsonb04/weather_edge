"""Durable paired baseline/challenger evidence (Task 7).

This module is the ONLY forecaster-side consumer of ``google_runtime_blend``
outside its own module and tests. It composes the permanent EMOS baseline the
caller supplies with the ephemeral Google runtime store to
compute the fixed, versioned research challenger, and persists ONLY the
derived evidence -- station/target/issue identity, policy version, baseline
mu/sigma, challenger mu/sigma, and the fixed action -- into a small durable
table in the caller's own (permanent) database connection. It never writes a
raw Google high, gap, response body, conditions text, URL, key, or token
(spec section 7.2/7.3): the table's own column set is the enforcement
boundary, not just application-level filtering.

Trading-side code never imports this module or ``google_runtime_blend``
directly -- the two projects deliberately do not import each other (same
convention as ``cities.py``/``settlement_calendar``). It reads the durable
table this module writes via a plain SQL query, exactly like
``SfoForecasterAdapter`` already reads every other forecaster-owned table in
``weather.db``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from cities import CityConfig
from google_runtime_blend import challenger_from_runtime_high, derive_station_day_high
from google_weather_store import GoogleRuntimeStore

PAIRED_EVIDENCE_TABLE = "google_challenger_research_baseline"

_COLUMNS = (
    "station_id",
    "target_date",
    "issued_at",
    "policy_version",
    "baseline_mu",
    "baseline_sigma",
    "challenger_mu",
    "challenger_sigma",
    "action",
)

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {PAIRED_EVIDENCE_TABLE} (
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    baseline_mu REAL NOT NULL,
    baseline_sigma REAL NOT NULL,
    challenger_mu REAL,
    challenger_sigma REAL NOT NULL,
    action TEXT NOT NULL,
    PRIMARY KEY(station_id, target_date, issued_at, policy_version)
)
"""


def ensure_paired_evidence_table(conn: sqlite3.Connection) -> None:
    """Create the durable paired-evidence table if it does not exist yet."""

    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


def _coerce_finite_float(name: str, value: object) -> float:
    """Coerce an EMOS-derived scalar (including numpy) to a plain float.

    ``google_runtime_blend._finite_float`` guards against NaN-poisoning with
    an exact ``type(value) not in (int, float)`` check, so a numpy.float64 or
    any other numeric-like non-builtin type is rejected before it reaches the
    formula (T7-2). EMOS fitting can hand back numpy scalars; convert them to
    a plain ``float`` here, at the paired-evidence boundary, before calling
    into the frozen challenger formula. A non-finite value is still rejected
    (raises ``ValueError``), matching the formula's own fail-closed guard.
    """

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be convertible to a finite float") from exc
    return number


def derive_and_record_paired_evidence(
    conn: sqlite3.Connection,
    *,
    city: CityConfig,
    target_date: str,
    baseline_mu: float,
    baseline_sigma: float,
    runtime_store: GoogleRuntimeStore,
    now: datetime | None = None,
) -> dict | None:
    """Compute and durably persist ONE paired baseline/challenger evidence row.

    Fails closed (returns ``None``, writes nothing) when Google runtime
    evidence is missing or was derived from an incomplete fixed-standard
    station day -- the permanent EMOS baseline passed in is never mutated or
    returned by this function. Idempotent: deriving the same
    (station, target_date, issued_at, policy_version) identity twice persists
    one row and returns the same evidence both times.
    """

    baseline_mu = _coerce_finite_float("baseline_mu", baseline_mu)
    baseline_sigma = _coerce_finite_float("baseline_sigma", baseline_sigma)

    runtime_high = derive_station_day_high(
        runtime_store, city=city, target_date=target_date, now=now
    )
    challenger = challenger_from_runtime_high(
        runtime_high, baseline_mu=baseline_mu, baseline_sigma=baseline_sigma
    )
    if challenger is None:
        return None

    issued_at = runtime_high.issued_at.isoformat()
    ensure_paired_evidence_table(conn)
    row = {
        "station_id": city.nws_station_id,
        "target_date": target_date,
        "issued_at": issued_at,
        "policy_version": challenger.policy_version,
        "baseline_mu": baseline_mu,
        "baseline_sigma": baseline_sigma,
        "challenger_mu": challenger.mu,
        "challenger_sigma": challenger.sigma,
        "action": challenger.action,
    }
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {PAIRED_EVIDENCE_TABLE} (
            {", ".join(_COLUMNS)}
        ) VALUES ({", ".join("?" for _ in _COLUMNS)})
        """,
        tuple(row[column] for column in _COLUMNS),
    )
    conn.commit()
    # W2 (Task 7 review, MEDIUM): INSERT OR IGNORE silently skips the write
    # when this exact (station, target_date, issued_at, policy_version)
    # identity is already recorded -- e.g. the Google runtime evidence is
    # unchanged but the permanent EMOS baseline refit between two calls
    # inside the same Google issue window. Re-SELECT and return the row
    # that is actually durably stored rather than the freshly computed
    # dict above, which may now be stale relative to the database.
    stored = conn.execute(
        f"""
        SELECT {", ".join(_COLUMNS)}
        FROM {PAIRED_EVIDENCE_TABLE}
        WHERE station_id = ? AND target_date = ? AND issued_at = ?
          AND policy_version = ?
        """,
        (row["station_id"], row["target_date"], row["issued_at"], row["policy_version"]),
    ).fetchone()
    return dict(zip(_COLUMNS, stored))


def latest_paired_evidence(
    conn: sqlite3.Connection, *, station_id: str, target_date: str
) -> dict | None:
    """Read the freshest persisted paired-evidence row for one city/target.

    A plain SQL read with no side effects; returns ``None`` when the table or
    a matching row does not exist yet (a brand-new database has neither).
    """

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (PAIRED_EVIDENCE_TABLE,),
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute(
        f"""
        SELECT {", ".join(_COLUMNS)}
        FROM {PAIRED_EVIDENCE_TABLE}
        WHERE station_id = ? AND target_date = ?
        ORDER BY issued_at DESC
        LIMIT 1
        """,
        (station_id, target_date),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_COLUMNS, row))
