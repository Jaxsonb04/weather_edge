"""DB-level backstop for account-scoped concurrent active-order admission.

The application check is a check-then-insert across separate SQLite
connections. The partial UNIQUE index therefore enforces one active order for
an account/date/market/side across filled, resting, partially filled, and
partial-expired exposure. Separate accounts and opposite-side boxes remain
independent, and re-entry is allowed after close or settlement.
"""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from sfo_kalshi_quant.account import RESEARCH_ACCOUNT_ID, SHARED_ACCOUNT_ID
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.research_policy import MOTION_POLICY, TARGET_POLICY
from sfo_kalshi_quant.store.schema import OPEN_POSITION_GUARD_INDEX
from test_research_sleeves import _insert_research_order


def _decision(
    ticker: str = "KXHIGHTSFO-26JUN19-B72.5",
    *,
    side: str = "NO",
) -> TradeDecision:
    action = "BUY_NO" if side.upper() == "NO" else "BUY_YES"
    return TradeDecision(
        ticker=ticker,
        label="72° to 73°",
        action=action,
        approved=True,
        probability=0.70,
        probability_lcb=0.62,
        yes_bid=0.48,
        yes_ask=0.50,
        spread=0.02,
        fee_per_contract=0.02,
        cost_per_contract=0.52,
        edge=0.18,
        edge_lcb=0.10,
        kelly_fraction=0.01,
        recommended_contracts=3.0,
        expected_profit=0.5,
        reasons=[],
        trade_quality_score=80.0,
        side=side,
        strike_type="between",
        floor_strike=72.0,
        cap_strike=73.0,
    )


def _open_count(store: PaperStore, ticker: str, side: str, account_id: str) -> int:
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM paper_orders
            WHERE target_date = ? AND market_ticker = ?
              AND UPPER(COALESCE(side, 'YES')) = ?
              AND COALESCE(account_id, 'paper-shared') = ?
              AND status IN (
                  'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
                  'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
              )
              AND settled_at IS NULL AND closed_at IS NULL
            """,
            ("2026-06-19", ticker, side.upper(), account_id),
        ).fetchone()
    return int(row[0])


def test_guard_index_is_created_on_init():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        with store.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            before = conn.execute(
                "SELECT sql, rootpage FROM sqlite_master WHERE type='index' "
                "AND name='ux_paper_orders_open_market_side_profile'"
            ).fetchone()
        PaperStore(db_path)
        with store.connect() as conn:
            after = conn.execute(
                "SELECT sql, rootpage FROM sqlite_master WHERE type='index' "
                "AND name='ux_paper_orders_open_market_side_profile'"
            ).fetchone()
        assert "ux_paper_orders_open_market_side_profile" in names
        assert after == before


def test_duplicate_open_same_market_side_account_is_rejected():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = store.record_paper_order("2026-06-19", _decision(), risk_profile="research")
        second = store.record_paper_order("2026-06-19", _decision(), risk_profile="research")

        assert isinstance(first, int)
        # The backstop rejects the concurrent duplicate; the store signals None.
        assert second is None
        assert (
            _open_count(
                store,
                "KXHIGHTSFO-26JUN19-B72.5",
                "NO",
                RESEARCH_ACCOUNT_ID,
            )
            == 1
        )


def test_same_market_other_account_is_independent():
    # Live and legacy research shadow accounts have independent active guards.
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        research = store.record_paper_order("2026-06-19", _decision(), risk_profile="research")
        live = store.record_paper_order("2026-06-19", _decision(), risk_profile="live")

        assert isinstance(research, int)
        assert isinstance(live, int)
        assert (
            _open_count(
                store,
                "KXHIGHTSFO-26JUN19-B72.5",
                "NO",
                RESEARCH_ACCOUNT_ID,
            )
            == 1
        )
        assert (
            _open_count(
                store,
                "KXHIGHTSFO-26JUN19-B72.5",
                "NO",
                SHARED_ACCOUNT_ID,
            )
            == 1
        )


def test_arbitrage_yes_and_no_box_on_one_market_is_allowed():
    # The index is side-inclusive precisely so a deliberate YES+NO box stays legal.
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        no_leg = store.record_paper_order(
            "2026-06-19", _decision(side="NO"), risk_profile="research", group_id="ARB-test"
        )
        yes_leg = store.record_paper_order(
            "2026-06-19", _decision(side="YES"), risk_profile="research", group_id="ARB-test"
        )

        assert isinstance(no_leg, int)
        assert isinstance(yes_leg, int)
        assert (
            _open_count(
                store,
                "KXHIGHTSFO-26JUN19-B72.5",
                "NO",
                RESEARCH_ACCOUNT_ID,
            )
            == 1
        )
        assert (
            _open_count(
                store,
                "KXHIGHTSFO-26JUN19-B72.5",
                "YES",
                RESEARCH_ACCOUNT_ID,
            )
            == 1
        )


def test_reentry_after_close_is_allowed():
    # The index is partial on the open lifecycle, so re-entry after a close works.
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = store.record_paper_order("2026-06-19", _decision(), risk_profile="research")
        store.close_paper_order(first, 0.5)
        second = store.record_paper_order("2026-06-19", _decision(), risk_profile="research")

        assert isinstance(first, int)
        assert isinstance(second, int)
        assert (
            _open_count(
                store,
                "KXHIGHTSFO-26JUN19-B72.5",
                "NO",
                RESEARCH_ACCOUNT_ID,
            )
            == 1
        )


@pytest.mark.parametrize(
    "status",
    (
        "PAPER_FILLED",
        "PAPER_LIMIT_RESTING",
        "PAPER_PARTIALLY_FILLED",
        "PAPER_PARTIAL_EXPIRED",
    ),
)
def test_every_active_status_is_counted_and_uniquely_guarded(
    tmp_path: Path,
    status: str,
) -> None:
    store = PaperStore(tmp_path / f"{status}.db")
    first = store.record_paper_order(
        "2026-06-19",
        _decision(),
        risk_profile="research",
        status=status,
    )
    second = store.record_paper_order(
        "2026-06-19",
        _decision(),
        risk_profile="research",
        status=status,
    )

    assert isinstance(first, int)
    assert second is None
    assert (
        _open_count(
            store,
            "KXHIGHTSFO-26JUN19-B72.5",
            "NO",
            RESEARCH_ACCOUNT_ID,
        )
        == 1
    )


def test_target_and_motion_can_hold_same_market_but_same_account_cannot(
    tmp_path: Path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")
    ticker = "KXHIGHTSFO-26JUL19-B80.5"
    with store.connect() as conn:
        target_id = _insert_research_order(
            conn,
            ticker=ticker,
            account_id=TARGET_POLICY.account_id,
            sleeve=TARGET_POLICY.sleeve.value,
            policy_version=TARGET_POLICY.policy_version,
            policy_fingerprint=TARGET_POLICY.policy_fingerprint,
        )
        motion_id = _insert_research_order(
            conn,
            ticker=ticker,
            account_id=MOTION_POLICY.account_id,
            sleeve=MOTION_POLICY.sleeve.value,
            policy_version=MOTION_POLICY.policy_version,
            policy_fingerprint=MOTION_POLICY.policy_fingerprint,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_research_order(
                conn,
                ticker=ticker,
                account_id=TARGET_POLICY.account_id,
                sleeve=TARGET_POLICY.sleeve.value,
                policy_version=TARGET_POLICY.policy_version,
                policy_fingerprint=TARGET_POLICY.policy_fingerprint,
            )

        rows = conn.execute(
            "SELECT id, account_id FROM paper_orders WHERE market_ticker=? ORDER BY id",
            (ticker,),
        ).fetchall()
    assert rows == [
        (target_id, TARGET_POLICY.account_id),
        (motion_id, MOTION_POLICY.account_id),
    ]


def test_account_guard_migration_fails_closed_before_dropping_legacy_index(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-duplicates.db"
    store = PaperStore(db_path)
    ticker = "KXHIGHTSFO-26JUL19-B81.5"
    legacy_index = """
        CREATE UNIQUE INDEX ux_paper_orders_open_market_side_profile
        ON paper_orders (
            target_date, market_ticker, UPPER(COALESCE(side, 'YES')),
            COALESCE(risk_profile, 'live')
        )
        WHERE status IN (
            'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
            'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
        ) AND settled_at IS NULL AND closed_at IS NULL
    """
    with store.connect() as conn:
        conn.execute("DROP INDEX ux_paper_orders_open_market_side_profile")
        conn.execute(legacy_index)
        _insert_research_order(
            conn,
            ticker=ticker,
            account_id="paper-shared",
            sleeve=None,
            policy_version=None,
            policy_fingerprint=None,
            risk_profile="live",
        )
        _insert_research_order(
            conn,
            ticker=ticker,
            account_id="paper-shared",
            sleeve=None,
            policy_version=None,
            policy_fingerprint=None,
            risk_profile="research",
        )

    with pytest.raises(sqlite3.IntegrityError, match="account-scoped open-position"):
        PaperStore(db_path)

    with sqlite3.connect(db_path) as conn:
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ux_paper_orders_open_market_side_profile'"
        ).fetchone()[0]
    assert "risk_profile" in index_sql
    assert "account_id" not in index_sql


def _canonical_guard_sql(sql: str) -> str:
    return " ".join(sql.replace(" IF NOT EXISTS", "").split())


@pytest.mark.parametrize(
    "malformed_sql",
    [
        OPEN_POSITION_GUARD_INDEX.replace("        target_date,\n", ""),
        OPEN_POSITION_GUARD_INDEX.replace("        market_ticker,\n", ""),
        OPEN_POSITION_GUARD_INDEX.replace(
            ",\n        UPPER(COALESCE(side, 'YES'))", ""
        ),
        OPEN_POSITION_GUARD_INDEX.replace("        'PAPER_FILLED', ", "        "),
        OPEN_POSITION_GUARD_INDEX.replace("'PAPER_LIMIT_RESTING',\n", "\n"),
        OPEN_POSITION_GUARD_INDEX.replace("      AND settled_at IS NULL\n", ""),
        OPEN_POSITION_GUARD_INDEX.replace("      AND closed_at IS NULL\n", ""),
        OPEN_POSITION_GUARD_INDEX.replace(
            "CREATE UNIQUE INDEX", "CREATE INDEX"
        ),
        OPEN_POSITION_GUARD_INDEX.replace("'paper-shared'", "'PAPER-SHARED'"),
        OPEN_POSITION_GUARD_INDEX.replace("'PAPER_FILLED'", "'paper_filled'"),
    ],
    ids=[
        "missing-target-date",
        "missing-market-ticker",
        "missing-side-expression",
        "missing-filled-status",
        "missing-resting-status",
        "missing-unsettled-predicate",
        "missing-unclosed-predicate",
        "not-unique",
        "changed-account-literal-case",
        "changed-status-literal-case",
    ],
)
def test_init_replaces_every_noncanonical_same_name_guard(
    tmp_path: Path,
    malformed_sql: str,
) -> None:
    db_path = tmp_path / "malformed-guard.db"
    store = PaperStore(db_path)
    with store.connect() as conn:
        conn.execute("DROP INDEX ux_paper_orders_open_market_side_profile")
        conn.execute(malformed_sql)

    PaperStore(db_path)

    with sqlite3.connect(db_path) as conn:
        stored_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ux_paper_orders_open_market_side_profile'"
        ).fetchone()[0]
        index_list_row = next(
            row
            for row in conn.execute("PRAGMA index_list(paper_orders)").fetchall()
            if row[1] == "ux_paper_orders_open_market_side_profile"
        )
        key_rows = conn.execute(
            "PRAGMA index_xinfo(ux_paper_orders_open_market_side_profile)"
        ).fetchall()

    assert _canonical_guard_sql(stored_sql) == _canonical_guard_sql(
        OPEN_POSITION_GUARD_INDEX
    )
    assert index_list_row[2] == 1  # unique
    assert index_list_row[4] == 1  # partial
    assert [row[1] for row in key_rows] == [-2, 2, 3, -2, -1]
    assert [row[5] for row in key_rows] == [1, 1, 1, 1, 0]


def test_guard_recreate_failure_rolls_back_to_the_previous_index(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "recreate-failure.db"
    store = PaperStore(db_path)
    malformed_sql = OPEN_POSITION_GUARD_INDEX.replace("        target_date,\n", "")
    with store.connect() as conn:
        conn.execute("DROP INDEX ux_paper_orders_open_market_side_profile")
        conn.execute(malformed_sql)

    denied = PaperStore(db_path, init=False)
    normal_connect = denied.connect

    def connect_denying_guard_create() -> sqlite3.Connection:
        conn = normal_connect()

        def authorizer(
            action: int,
            arg1: str | None,
            _arg2: str | None,
            _db_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            if (
                action == sqlite3.SQLITE_CREATE_INDEX
                and arg1 == "ux_paper_orders_open_market_side_profile"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    denied.connect = connect_denying_guard_create  # type: ignore[method-assign]
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        denied.init()

    with sqlite3.connect(db_path) as conn:
        stored_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ux_paper_orders_open_market_side_profile'"
        ).fetchone()[0]
    assert _canonical_guard_sql(stored_sql) == _canonical_guard_sql(malformed_sql)
