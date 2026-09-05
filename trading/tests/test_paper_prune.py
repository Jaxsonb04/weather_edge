"""Decision-snapshot retention: full window intact, dedup window keeps the
last row per market-side-day plus approvals, old rejections drop."""

import argparse
import contextlib
import random
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.cli import cmd_paper_prune


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


def _context(conn, created_at):
    return conn.execute(
        "INSERT INTO scan_context_snapshots "
        "(created_at, target_date, prediction_features_json) VALUES (?, '2026-06-01', '{}')",
        (created_at,),
    ).lastrowid


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


def test_prune_removes_only_unreferenced_contexts_without_dangling_refs():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            dropped_context = _context(conn, "2026-01-01T00:00:00+00:00")
            kept_context = _context(conn, "2026-01-01T00:05:00+00:00")
            _insert(conn, "x", "T-DROP", "YES", 0)
            conn.execute(
                "UPDATE decision_snapshots SET created_at=datetime('now', '-100 days'), "
                "scan_context_id=? WHERE market_ticker='T-DROP'",
                (dropped_context,),
            )
            _insert(conn, "x", "T-KEEP", "YES", 1)
            conn.execute(
                "UPDATE decision_snapshots SET created_at=datetime('now', '-100 days'), "
                "scan_context_id=? WHERE market_ticker='T-KEEP'",
                (kept_context,),
            )

        result = store.prune_decision_snapshots(full_days=7, dedup_days=45)

        with store.connect() as conn:
            remaining_contexts = {
                row[0] for row in conn.execute("SELECT id FROM scan_context_snapshots")
            }
            dangling = conn.execute(
                "SELECT COUNT(*) FROM decision_snapshots d LEFT JOIN scan_context_snapshots c "
                "ON c.id=d.scan_context_id WHERE d.scan_context_id IS NOT NULL AND c.id IS NULL"
            ).fetchone()[0]
        assert result["contexts_dropped"] == 1
        assert remaining_contexts == {kept_context}
        assert dangling == 0


def test_prune_cli_reports_context_rows_dropped(capsys):
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p.db"
        store = PaperStore(db_path)
        with store.connect() as conn:
            _context(conn, "2026-01-01T00:00:00+00:00")

        assert cmd_paper_prune(
            argparse.Namespace(
                db_path=db_path,
                full_days=7,
                dedup_days=45,
                no_color=True,
            )
        ) == 0

    assert "1 contexts dropped" in capsys.readouterr().out


def test_prune_caps_archive_backed_streams_and_keeps_referenced_parents():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            old_forecast = conn.execute(
                "INSERT INTO forecast_snapshots "
                "(created_at, target_date, predicted_high_f, raw_json) "
                "VALUES (datetime('now', '-100 days'), '2026-06-01', 65, '{}')"
            ).lastrowid
            referenced_forecast = conn.execute(
                "INSERT INTO forecast_snapshots "
                "(created_at, target_date, predicted_high_f, raw_json) "
                "VALUES (datetime('now', '-100 days'), '2026-06-01', 66, '{}')"
            ).lastrowid
            old_market = conn.execute(
                "INSERT INTO market_snapshots "
                "(created_at, event_ticker, target_date, raw_json) "
                "VALUES (datetime('now', '-100 days'), 'E-OLD', '2026-06-01', '{}')"
            ).lastrowid
            referenced_market = conn.execute(
                "INSERT INTO market_snapshots "
                "(created_at, event_ticker, target_date, raw_json) "
                "VALUES (datetime('now', '-100 days'), 'E-KEEP', '2026-06-01', '{}')"
            ).lastrowid
            conn.execute(
                "INSERT INTO scan_context_snapshots "
                "(created_at, target_date, forecast_snapshot_id, market_snapshot_id, "
                "prediction_features_json, source_context_hash) "
                "VALUES (datetime('now', '-100 days'), '2026-06-01', ?, ?, '{}', 'keep')",
                (referenced_forecast, referenced_market),
            )
            for age in (100, 10):
                conn.execute(
                    "INSERT INTO probability_snapshots "
                    "(created_at, target_date, market_ticker, label, probability, "
                    "lower_confidence, empirical_probability, normal_probability, effective_n) "
                    "VALUES (datetime('now', ?), '2026-06-01', 'T-P', 'l', "
                    "0.5, 0.4, 0.5, 0.5, 1)",
                    (f"-{age} days",),
                )
                conn.execute(
                    "INSERT INTO paper_monitor_snapshots "
                    "(created_at, order_id, target_date, market_ticker, side, action) "
                    "VALUES (datetime('now', ?), 1, '2026-06-01', 'T-M', 'YES', 'HOLD')",
                    (f"-{age} days",),
                )

        result = store.prune_decision_snapshots(full_days=7, dedup_days=45)

        assert result["probabilities_dropped"] == 1
        assert result["monitor_snapshots_dropped"] == 1
        assert result["forecast_snapshots_dropped"] == 1
        assert result["market_snapshots_dropped"] == 1
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM probability_snapshots"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM paper_monitor_snapshots"
            ).fetchone()[0] == 1
            assert {
                row[0] for row in conn.execute("SELECT id FROM forecast_snapshots")
            } == {referenced_forecast}
            assert {
                row[0] for row in conn.execute("SELECT id FROM market_snapshots")
            } == {referenced_market}
            assert old_forecast != referenced_forecast
            assert old_market != referenced_market


# ---------------------------------------------------------------------------
# 2026-07-27 audit (F.8 performance rewrite, F.9 dedup key). The rewrite bounded
# three previously-unbounded anti-joins and batched the deletes. These tests pin
# the behaviour that changed, and the behaviour that deliberately did not.
# ---------------------------------------------------------------------------


def _insert_profiled(conn, ticker, side, profile, approved=0):
    """Insert one decision row carrying an explicit risk_profile."""

    conn.execute(
        """
        INSERT INTO decision_snapshots (
            created_at, target_date, market_ticker, label, action, side,
            risk_profile, approved, signal_approved, probability,
            probability_lcb, yes_bid, yes_ask, spread, fee_per_contract,
            cost_per_contract, edge, edge_lcb, kelly_fraction,
            recommended_contracts, recommended_spend, expected_profit,
            trade_quality_score, reasons_json
        ) VALUES ('x', '2026-06-01', ?, 'l', 'BUY_YES', ?, ?, ?, 0, 0.5, 0.4,
                  0.4, 0.42, 0.02, 0.01, 0.43, 0.07, 0.0, 0.01, 1, 0.43, 0.07,
                  10, '[]')
        """,
        (ticker, side, profile, approved),
    )


def _age_all(conn, days):
    """Push every row to `days` old, preserving id order in created_at."""

    conn.execute(
        "UPDATE decision_snapshots "
        "SET created_at = datetime('now', ?, '+' || id || ' seconds')",
        (f"-{days} days",),
    )


def test_prune_dedup_key_separates_risk_profiles():
    """F.9: each book keeps its own end-of-day rejection evidence.

    The dedup key omitted risk_profile while every analysis sampler partitions
    by it, so the single surviving "last row per market/side/day" was whichever
    book happened to write last and the other book's evidence was destroyed.
    """

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            for profile in ("target", "motion"):
                for _ in range(2):
                    _insert_profiled(conn, "T-P", "YES", profile)
            _age_all(conn, 10)

        result = store.prune_decision_snapshots(full_days=7, dedup_days=45)

        with store.connect() as conn:
            surviving = dict(
                conn.execute(
                    "SELECT risk_profile, COUNT(*) FROM decision_snapshots "
                    "GROUP BY risk_profile"
                )
            )
        # One row per profile survives, not one row overall.
        assert result["deduped"] == 2
        assert surviving == {"target": 1, "motion": 1}


def test_prune_dedup_window_bound_retains_more_not_less():
    """F.8: bounding the MAX(id) grouping is conservative by construction.

    The old grouping scanned the WHOLE table, so a protected in-full-window row
    became its group's max and every older row in the dedup window lost its
    reprieve. Bounding the grouping to the same window as the candidate rows
    means the window's own newest row survives. The rewrite therefore deletes
    strictly FEWER rows here -- never more.
    """

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            # Two rejections inside the dedup window...
            _insert_profiled(conn, "T-W", "YES", "target")
            _insert_profiled(conn, "T-W", "YES", "target")
            conn.execute(
                "UPDATE decision_snapshots "
                "SET created_at = datetime('now', '-10 days', "
                "'+' || id || ' seconds')"
            )
            # ...and a newer one that retention must not touch at all. Under the
            # old unbounded grouping this row was the group max, which deleted
            # BOTH rows above.
            _insert_profiled(conn, "T-W", "YES", "target")
            conn.execute(
                "UPDATE decision_snapshots SET created_at = datetime('now', "
                "'-3 days') WHERE id = (SELECT MAX(id) FROM decision_snapshots)"
            )

        result = store.prune_decision_snapshots(full_days=7, dedup_days=45)

        with store.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM decision_snapshots"
            ).fetchone()[0]
        assert result["deduped"] == 1
        # The protected recent row plus the window's own newest row.
        assert remaining == 2


def test_prune_batching_is_equivalent_to_a_single_pass():
    """Batch size is a performance knob and must not change the outcome."""

    def _build(path):
        store = PaperStore(path)
        with store.connect() as conn:
            for index in range(12):
                _insert_profiled(conn, f"T-{index % 3}", "YES", "target")
            _age_all(conn, 10)
        return store

    with TemporaryDirectory() as tmp:
        batched = _build(Path(tmp) / "batched.db")
        single = _build(Path(tmp) / "single.db")

        small = batched.prune_decision_snapshots(
            full_days=7, dedup_days=45, batch_limit=1
        )
        large = single.prune_decision_snapshots(
            full_days=7, dedup_days=45, batch_limit=10_000
        )

        assert small == large

        def _survivors(store):
            with store.connect() as conn:
                return sorted(
                    conn.execute(
                        "SELECT market_ticker, side, risk_profile "
                        "FROM decision_snapshots"
                    )
                )

        assert _survivors(batched) == _survivors(single)


def test_prune_materializes_the_expensive_dedup_probe_once():
    """A small batch must not rescan the full dedup window per commit."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            for index in range(12):
                _insert_profiled(conn, f"T-{index % 3}", "YES", "target")
            _age_all(conn, 10)

        statements: list[str] = []
        opened = store.connect

        @contextlib.contextmanager
        def _traced():
            with opened() as conn:
                conn.set_trace_callback(statements.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        store.connect = _traced
        try:
            result = store.prune_decision_snapshots(
                full_days=7,
                dedup_days=45,
                batch_limit=1,
                batch_pause_seconds=0,
            )
        finally:
            store.connect = opened

        candidate_materializations = [
            sql
            for sql in statements
            if sql.strip().upper().startswith(
                "INSERT INTO WEATHEREDGE_PRUNE_DECISION_IDS"
            )
        ]
        assert result["deduped"] == 9
        assert len(candidate_materializations) == 1


def test_prune_batch_loop_terminates_on_exact_multiple_of_batch_limit():
    """The loop exits on `removed < batch_limit`; an exact multiple is the
    boundary where a naive implementation either stops early or spins."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            # 6 ancient rejections -> all droppable, an exact multiple of 3.
            for index in range(6):
                _insert_profiled(conn, f"T-{index}", "YES", "target")
            _age_all(conn, 100)

        result = store.prune_decision_snapshots(
            full_days=7, dedup_days=45, batch_limit=3
        )

        with store.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM decision_snapshots"
            ).fetchone()[0]
        assert result["dropped"] == 6
        assert remaining == 0


def test_prune_is_idempotent_so_an_interrupted_run_can_resume():
    """Per-batch commits mean a killed prune leaves durable progress. Re-running
    must therefore be a clean no-op rather than double-counting or erroring."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            for index in range(5):
                _insert_profiled(conn, f"T-{index}", "YES", "target")
            _age_all(conn, 100)

        first = store.prune_decision_snapshots(full_days=7, dedup_days=45)
        second = store.prune_decision_snapshots(full_days=7, dedup_days=45)

        assert first["dropped"] == 5
        assert all(value == 0 for value in second.values())


def test_prune_keeps_a_forecast_parent_referenced_only_by_a_decision_row():
    """The parent-orphan probes were rewritten from a NOT IN over a UNION to two
    correlated NOT EXISTS. Both reference paths must still protect a parent; a
    decision-only reference is the path the scan-context test does not cover."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            kept = conn.execute(
                "INSERT INTO forecast_snapshots "
                "(created_at, target_date, predicted_high_f, raw_json) VALUES "
                "(datetime('now', '-100 days'), '2026-06-01', 65, '{}')"
            ).lastrowid
            orphan = conn.execute(
                "INSERT INTO forecast_snapshots "
                "(created_at, target_date, predicted_high_f, raw_json) VALUES "
                "(datetime('now', '-100 days'), '2026-06-01', 66, '{}')"
            ).lastrowid
            # An APPROVED decision, so retention never deletes the child and the
            # parent stays genuinely referenced.
            _insert_profiled(conn, "T-F", "YES", "target", approved=1)
            conn.execute(
                "UPDATE decision_snapshots SET created_at = "
                "datetime('now', '-100 days'), forecast_snapshot_id = ?",
                (kept,),
            )

        store.prune_decision_snapshots(full_days=7, dedup_days=45)

        with store.connect() as conn:
            surviving = {
                row[0] for row in conn.execute("SELECT id FROM forecast_snapshots")
            }
            dangling = conn.execute(
                "SELECT COUNT(*) FROM decision_snapshots d "
                "LEFT JOIN forecast_snapshots f ON f.id = d.forecast_snapshot_id "
                "WHERE d.forecast_snapshot_id IS NOT NULL AND f.id IS NULL"
            ).fetchone()[0]
        assert surviving == {kept}
        assert orphan not in surviving
        assert dangling == 0


def test_prune_rejects_a_nonpositive_batch_limit():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        try:
            store.prune_decision_snapshots(
                full_days=7, dedup_days=45, batch_limit=0
            )
        except ValueError as exc:
            assert "batch_limit" in str(exc)
        else:
            raise AssertionError("expected ValueError for batch_limit=0")


def test_every_retention_delete_is_index_supported():
    """No retention DELETE may fall back to a scan or a sort.

    This is the regression guard for the 2026-07-27 timeout (F.8). Two ways to
    lose the fix are invisible in behavioural tests and were both hit while
    writing it: indexing a bare column while the query groups by an EXPRESSION,
    and probing a foreign-key column that carries no index at all. Either one
    silently restores the full scans that made the nightly unit exceed
    TimeoutStartSec, while every correctness test still passes.

    The statements are captured from the real prune via a trace callback rather
    than duplicated here, so this cannot drift from db.py.
    """

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            _insert_profiled(conn, "T-PLAN", "YES", "target")
            _insert_profiled(conn, "T-PLAN", "YES", "target")
            _age_all(conn, 10)

        statements: list[str] = []
        opened = store.connect

        @contextlib.contextmanager
        def _traced():
            with opened() as conn:
                conn.set_trace_callback(statements.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        store.connect = _traced
        try:
            store.prune_decision_snapshots(
                full_days=1, dedup_days=45, batch_limit=100
            )
        finally:
            store.connect = opened

        deletes = [
            sql
            for sql in statements
            if sql.strip().upper().startswith("DELETE FROM")
        ]
        # One per retained stream: decisions (dedup + drop), contexts,
        # probabilities, monitor snapshots, forecasts, markets.
        assert len(deletes) >= 7, f"expected every stream to be pruned: {deletes}"

        offenders = []
        with store.connect() as conn:
            for sql in deletes:
                plan = [
                    row[-1]
                    for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
                ]
                sorted_ = [line for line in plan if "TEMP B-TREE" in line]
                # "SCAN t" without USING is a full table scan; "SCAN t USING
                # ... INDEX" is an ordered index walk and is fine.
                scanned = [
                    line
                    for line in plan
                    if line.strip().startswith("SCAN") and "USING" not in line
                ]
                if sorted_ or scanned:
                    offenders.append((sql.split()[2], sorted_, scanned, plan))

        assert not offenders, "retention DELETEs lost index support:\n" + "\n".join(
            f"  {table}: sort={sort} scan={scan}\n    " + "\n    ".join(plan)
            for table, sort, scan, plan in offenders
        )


def test_rewritten_dedup_never_deletes_a_row_the_original_kept():
    """Property test over randomised production-shaped journals.

    The behavioural tests above assert hand-picked cases. This asserts the
    property that actually matters for a one-way deletion against a live
    journal: across many randomised shapes, the rewritten dedup must never
    delete a row the pre-2026-07-27 SQL would have kept. It is allowed -- and
    expected -- to retain MORE, both because the grouping is now bounded to the
    window and because risk_profile splits groups.

    Seeded, so a failure is reproducible rather than a flake.
    """

    original = """
        SELECT id FROM decision_snapshots
        WHERE created_at < :full AND created_at >= :dedup
          AND COALESCE(approved,0)=0 AND COALESCE(signal_approved,0)=0
          AND id NOT IN (SELECT MAX(id) FROM decision_snapshots
                         GROUP BY market_ticker, side, target_date)
    """
    shipped = """
        SELECT d.id FROM decision_snapshots AS d
        WHERE d.created_at < :full AND d.created_at >= :dedup
          AND COALESCE(d.approved,0)=0 AND COALESCE(d.signal_approved,0)=0
          AND EXISTS (SELECT 1 FROM decision_snapshots AS n
                      WHERE n.market_ticker=d.market_ticker AND n.side=d.side
                        AND n.target_date=d.target_date
                        AND COALESCE(n.risk_profile,'')
                            = COALESCE(d.risk_profile,'')
                        AND n.id>d.id
                        AND n.created_at < :full AND n.created_at >= :dedup)
    """

    rng = random.Random(20260727)
    tickers = ["KXHIGHNY", "KXHIGHLAX", "KXHIGHCHI"]
    days = ["2026-06-1%d" % d for d in range(4)]
    retained_more = 0

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "p.db")
        for _ in range(60):
            with store.connect() as conn:
                conn.execute("DELETE FROM decision_snapshots")
                for _ in range(rng.randint(20, 120)):
                    _insert_profiled(
                        conn,
                        rng.choice(tickers),
                        rng.choice(["YES", "NO"]),
                        rng.choice(["target", "motion", None]),
                        approved=rng.choice([0, 0, 0, 1]),
                    )
                conn.execute(
                    "UPDATE decision_snapshots SET target_date = ?, "
                    "created_at = datetime('now', '-' || (id % 60) || ' days', "
                    "'+' || id || ' seconds')",
                    (rng.choice(days),),
                )
                params = {}
                params["full"], params["dedup"] = conn.execute(
                    "SELECT datetime('now','-1 days'), datetime('now','-45 days')"
                ).fetchone()

                old_ids = {row[0] for row in conn.execute(original, params)}
                new_ids = {row[0] for row in conn.execute(shipped, params)}

                assert not (new_ids - old_ids), (
                    "rewrite deleted rows the original retained: "
                    f"{sorted(new_ids - old_ids)[:10]}"
                )
                retained_more += len(old_ids - new_ids)

    # If the rewrite never diverged at all the test would be proving nothing.
    assert retained_more > 0, "expected the bounded window to retain extra rows"
