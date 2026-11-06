"""Audit F7: the Overview city books must never combine separate paper accounts.

``cities_report._all_city_books`` grouped paper_orders by ``risk_profile``
alone. Research orders are always written with ``risk_profile='research'`` and
told apart only by their account/sleeve/policy identity, so ``books.research``
summed the active research account with every archived research era, and
``books.live`` summed Live Stability with the legacy shared ledger. These tests
pin the city books to the canonical published identity (``profile_identity``)
that every other public report already uses.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sfo_kalshi_quant import cities_report
from sfo_kalshi_quant.account import (
    LIVE_STABILITY_ACCOUNT_ID,
    RESEARCH_ACCOUNT_ID,
    SHARED_ACCOUNT_ID,
)
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V3,
    TARGET_POLICY_V6,
)


NOW = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
CUTOFF = "2026-10-31T20:00:00+00:00"
# Every account trades the same market, so only the identity columns can keep
# the books apart: a GROUP BY that dropped them would merge accounts in SQL.
LAX_TICKER = "KXHIGHLAX-26NOV02-B70.5"
SETTLED_AT = "2026-10-30T06:00:00+00:00"
BOOK_FIELDS = {
    "open_positions",
    "resting_orders",
    "open_exposure",
    "settled_orders",
    "settled_pnl",
}


def _research_identity(policy) -> dict:
    return {
        "account_id": policy.account_id,
        "research_sleeve": policy.sleeve.value,
        "research_policy_version": policy.policy_version,
        "policy_fingerprint": policy.policy_fingerprint,
    }


LIVE = {"account_id": LIVE_STABILITY_ACCOUNT_ID}

# (risk_profile, identity, status, realized_pnl); each row is one contract at
# $0.41 in LAX_TICKER.
LEDGER_ROWS = [
    # Live Stability, the active live account: +30.37 settled, nothing open.
    ("live", LIVE, "PAPER_SETTLED", 30.00),
    ("live", LIVE, "PAPER_SETTLED", -0.63),
    ("live", LIVE, "PAPER_SETTLED", 1.00),
    # The legacy shared live ledger: an explicit row and a pre-account NULL row.
    ("live", {"account_id": SHARED_ACCOUNT_ID}, "PAPER_SETTLED", -9.00),
    (None, {}, "PAPER_FILLED", None),
    # The active research account, whichever version TARGET_POLICY is.
    ("research", _research_identity(TARGET_POLICY), "PAPER_SETTLED", 2.50),
    ("research", _research_identity(TARGET_POLICY), "PAPER_LIMIT_RESTING", None),
    # Archived research ledgers: V6 still settling, V3 long closed, motion
    # holding a position, and the old research shadow account.
    ("research", _research_identity(TARGET_POLICY_V6), "PAPER_SETTLED", -21.00),
    ("research", _research_identity(TARGET_POLICY_V6), "PAPER_SETTLED", 3.30),
    ("research", _research_identity(TARGET_POLICY_V6), "PAPER_FILLED", None),
    ("research", _research_identity(TARGET_POLICY_V3), "PAPER_SETTLED", -27.00),
    ("research", _research_identity(MOTION_POLICY), "PAPER_FILLED", None),
    ("research", {"account_id": RESEARCH_ACCOUNT_ID}, "PAPER_SETTLED", -5.00),
]


def _seed_paper_db(db_path: Path, rows=LEDGER_ROWS) -> None:
    """Write rows through the real schema, identity triggers included."""

    PaperStore(db_path)
    with sqlite3.connect(db_path) as conn:
        for profile, identity, status, pnl in rows:
            values = {
                "created_at": "2026-10-25T00:00:00+00:00",
                "target_date": "2026-11-02",
                "market_ticker": LAX_TICKER,
                "label": "70-71",
                "action": "BUY",
                "risk_profile": profile,
                "side": "YES",
                "contracts": 1.0,
                "yes_ask": 0.40,
                "entry_price": 0.40,
                "fee_per_contract": 0.01,
                "cost_per_contract": 0.41,
                "probability": 0.5,
                "probability_lcb": 0.45,
                "edge": 0.05,
                "edge_lcb": 0.02,
                "expected_profit": 0.05,
                "status": status,
                "reasons_json": "[]",
                "settled_at": SETTLED_AT if status == "PAPER_SETTLED" else None,
                "realized_pnl": pnl,
                **identity,
            }
            columns = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            conn.execute(
                f"INSERT INTO paper_orders ({columns}) VALUES ({marks})",
                tuple(values.values()),
            )


def _city_books(db_path: Path) -> tuple[dict[str, dict], list[str]]:
    statements: list[str] = []
    with sqlite3.connect(db_path) as conn:
        conn.set_trace_callback(statements.append)
        books = cities_report._all_city_books(conn, CUTOFF)
    return books, statements


def _book(open_positions=0, resting_orders=0, open_exposure=0.0, settled_orders=0, settled_pnl=0.0):
    return {
        "open_positions": open_positions,
        "resting_orders": resting_orders,
        "open_exposure": open_exposure,
        "settled_orders": settled_orders,
        "settled_pnl": settled_pnl,
    }


def _minimal_paper_orders(conn: sqlite3.Connection, *, identity_columns: bool) -> None:
    """A trigger-free table: legacy rows predate the research identity triggers."""

    identity = (
        """,
            account_id TEXT,
            research_sleeve TEXT,
            research_policy_version TEXT,
            policy_fingerprint TEXT"""
        if identity_columns
        else ""
    )
    conn.execute(
        f"""
        CREATE TABLE paper_orders (
            market_ticker TEXT NOT NULL,
            risk_profile TEXT,
            contracts REAL NOT NULL,
            cost_per_contract REAL NOT NULL,
            status TEXT NOT NULL,
            settled_at TEXT,
            closed_at TEXT,
            realized_pnl REAL{identity}
        )
        """
    )


def test_live_and_research_books_hold_only_the_two_active_accounts():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        _seed_paper_db(db_path)
        books, statements = _city_books(db_path)

    lax = books["lax"]
    # Live Stability alone. The legacy shared ledger's -9.00 and its NULL-era
    # open contract used to land here and turn +30.37 into +21.37.
    assert lax["live"] == _book(settled_orders=3, settled_pnl=30.37)
    # The active research account alone: never V6, V3, motion or the shadow.
    assert lax["research"] == _book(
        resting_orders=1,
        open_exposure=0.41,
        settled_orders=1,
        settled_pnl=2.5,
    )
    # Still one grouped pass over paper_orders.
    assert len([sql for sql in statements if "FROM paper_orders" in sql]) == 1


def test_each_archived_ledger_keeps_its_own_labelled_book():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        _seed_paper_db(db_path)
        books, _statements = _city_books(db_path)

    archived = books["lax"]["archived"]
    assert archived == {
        "live-legacy": {
            **_book(open_positions=1, open_exposure=0.41, settled_orders=1, settled_pnl=-9.0),
            "status": "ARCHIVED_SETTLING",
        },
        "research-target-v6": {
            **_book(open_positions=1, open_exposure=0.41, settled_orders=2, settled_pnl=-17.7),
            "status": "ARCHIVED_SETTLING",
        },
        "research-target-v3": {
            **_book(settled_orders=1, settled_pnl=-27.0),
            "status": "ARCHIVED",
        },
        "research-motion": {
            **_book(open_positions=1, open_exposure=0.41),
            "status": "ARCHIVED_SETTLING",
        },
        "research": {
            **_book(settled_orders=1, settled_pnl=-5.0),
            "status": "ARCHIVED",
        },
    }
    # A city with no activity publishes the same shape with nothing archived.
    assert books["sfo"]["archived"] == {}
    assert books["sfo"]["live"] == _book()
    assert books["sfo"]["research"] == _book()


def test_partial_or_crossed_research_identity_is_published_in_no_book():
    """The canonical identity is all-or-nothing; one marker proves no account."""

    with sqlite3.connect(":memory:") as conn:
        _minimal_paper_orders(conn, identity_columns=True)
        conn.executemany(
            "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # Active account and policy, but no fingerprint.
                (
                    LAX_TICKER, "research", 1.0, 0.41, "PAPER_SETTLED", SETTLED_AT, None, 7.0,
                    TARGET_POLICY.account_id, TARGET_POLICY.sleeve.value,
                    TARGET_POLICY.policy_version, None,
                ),
                # Active account carrying the archived V6 policy fingerprint.
                (
                    LAX_TICKER, "research", 1.0, 0.41, "PAPER_FILLED", None, None, None,
                    TARGET_POLICY.account_id, TARGET_POLICY_V6.sleeve.value,
                    TARGET_POLICY_V6.policy_version, TARGET_POLICY_V6.policy_fingerprint,
                ),
            ],
        )
        books = cities_report._all_city_books(conn, CUTOFF)

    assert books["lax"]["research"] == _book()
    assert books["lax"]["live"] == _book()
    assert books["lax"]["archived"] == {}


def test_database_without_identity_columns_reads_as_archived_legacy_books():
    with sqlite3.connect(":memory:") as conn:
        _minimal_paper_orders(conn, identity_columns=False)
        conn.executemany(
            "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (LAX_TICKER, "live", 1.0, 0.41, "PAPER_SETTLED", SETTLED_AT, None, 1.5),
                (LAX_TICKER, None, 2.0, 0.25, "PAPER_FILLED", None, None, None),
                (LAX_TICKER, "research", 1.0, 0.41, "PAPER_SETTLED", SETTLED_AT, None, -2.0),
            ],
        )
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        books = cities_report._all_city_books(conn, CUTOFF)

    lax = books["lax"]
    # Pre-account rows belong to the legacy ledgers, not the active accounts.
    assert lax["live"] == _book()
    assert lax["research"] == _book()
    assert lax["archived"] == {
        "live-legacy": {
            **_book(open_positions=1, open_exposure=0.5, settled_orders=1, settled_pnl=1.5),
            "status": "ARCHIVED_SETTLING",
        },
        "research": {
            **_book(settled_orders=1, settled_pnl=-2.0),
            "status": "ARCHIVED",
        },
    }
    assert len([sql for sql in statements if "FROM paper_orders" in sql]) == 1


def test_blank_profile_resolves_by_account_not_the_process_environment(monkeypatch):
    # normalize_risk_profile_name('') fell back to PAPER_RISK_PROFILE, so the
    # same row moved between books depending on who ran the report.
    monkeypatch.setenv("PAPER_RISK_PROFILE", "research")
    with sqlite3.connect(":memory:") as conn:
        _minimal_paper_orders(conn, identity_columns=True)
        conn.execute(
            "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                LAX_TICKER, "", 1.0, 0.41, "PAPER_SETTLED", SETTLED_AT, None, 4.0,
                LIVE_STABILITY_ACCOUNT_ID, None, None, None,
            ),
        )
        books = cities_report._all_city_books(conn, CUTOFF)

    assert books["lax"]["live"] == _book(settled_orders=1, settled_pnl=4.0)
    assert books["lax"]["research"] == _book()


def test_published_payload_names_the_account_behind_each_active_book():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        _seed_paper_db(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()
        payload = cities_report.build_cities_data(forecaster_root, db_path, now=NOW)

    assert payload["book_accounts"] == {
        "live": {
            "status": "ACTIVE",
            "profile_key": "live",
            "account_id": LIVE_STABILITY_ACCOUNT_ID,
        },
        "research": {
            "status": "ACTIVE",
            "profile_key": "research-target",
            "account_id": TARGET_POLICY.account_id,
            "research_policy_version": TARGET_POLICY.policy_version,
        },
    }
    lax = next(city for city in payload["cities"] if city["slug"] == "lax")
    # Backward compatible: the site's two slots keep exactly their five fields;
    # archived ledgers are an added key, never folded into either slot.
    assert set(lax["books"]) == {
        "live",
        "research",
        "archived",
        "decisions_24h",
        "approved_24h",
    }
    assert set(lax["books"]["live"]) == BOOK_FIELDS
    assert set(lax["books"]["research"]) == BOOK_FIELDS
    assert lax["books"]["live"]["settled_pnl"] == 30.37
    assert lax["books"]["research"]["settled_pnl"] == 2.5
    assert "archived" in payload["note"]
    json.dumps(payload)
