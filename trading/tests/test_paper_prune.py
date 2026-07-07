"""Decision-snapshot retention: full window intact, dedup window keeps the
last row per market-side-day plus approvals, old rejections drop."""

from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.db import PaperStore


def _insert(conn, created_at, ticker, side, approved, signal_approved=0):
    conn.execute(
        """
        INSERT INTO decision_snapshots (
            created_at, target_date, market_ticker, label, action, side,
            approved, signal_approved, probability, probability_lcb, yes_bid, yes_ask,
            spread, fee_per_contract, cost_per_contract, edge, edge_lcb,
            kelly_fraction, recommended_contracts, recommended_spend,
            expected_profit, trade_quality_score, reasons_json
        ) VALUES (?, '2026-06-01', ?, 'l', 'BUY_YES', ?, ?, ?, 0.5, 0.4, 0.4, 0.42,
                  0.02, 0.01, 0.43, 0.07, 0.0, 0.01, 1, 0.43, 0.07, 10, '[]')
        """,
        (created_at, ticker, side, approved, signal_approved),
    )


def test_prune_keeps_recent_approved_and_last_per_day():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            # Recent (inside full window): kept regardless.
            _insert(conn, "datetime('now')", "T-A", "YES", 0)
            conn.execute(
                "UPDATE decision_snapshots SET created_at = datetime('now') WHERE market_ticker='T-A'"
            )
            # Mid window (10 days old): three rejections same market/side -> keep last only.
            for i in range(3):
                _insert(conn, f"2026-06-0{i+1}T0{i}:00:00", "T-B", "NO", 0)
            conn.execute(
                "UPDATE decision_snapshots SET created_at = datetime('now', '-10 days', '+' || id || ' seconds') WHERE market_ticker='T-B'"
            )
            # Mid window approved: kept.
            _insert(conn, "x", "T-C", "YES", 1)
            conn.execute(
                "UPDATE decision_snapshots SET created_at = datetime('now', '-10 days') WHERE market_ticker='T-C'"
            )
            # Ancient rejection: dropped. Ancient approved: kept.
            _insert(conn, "x", "T-D", "NO", 0)
            conn.execute(
                "UPDATE decision_snapshots SET created_at = datetime('now', '-100 days') WHERE market_ticker='T-D' AND approved=0"
            )
            _insert(conn, "x", "T-E", "YES", 1)
            conn.execute(
                "UPDATE decision_snapshots SET created_at = datetime('now', '-100 days') WHERE market_ticker='T-E'"
            )

        result = store.prune_decision_snapshots(full_days=7, dedup_days=45)
        assert result["deduped"] == 2  # two older T-B duplicates
        assert result["dropped"] == 1  # ancient T-D rejection

        with store.connect() as conn:
            remaining = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT market_ticker, COUNT(*) FROM decision_snapshots GROUP BY 1"
                )
            }
        assert remaining == {"T-A": 1, "T-B": 1, "T-C": 1, "T-E": 1}
