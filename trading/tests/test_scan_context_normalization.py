from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest

from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.consensus import MarketConsensus
from sfo_kalshi_quant.db import PaperStore, _decision_diagnostics_payload
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot, TradeDecision
from sfo_kalshi_quant.prediction_features import build_prediction_feature_snapshot

from support import pre_resolution_event


def _insert_decision_with_context(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    scan_context_id: int,
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO decision_snapshots (
                created_at, target_date, market_ticker, label, action, side,
                approved, probability, probability_lcb, yes_bid, yes_ask, spread,
                fee_per_contract, cost_per_contract, edge, edge_lcb, kelly_fraction,
                recommended_contracts, recommended_spend, expected_profit,
                trade_quality_score, intraday_is_complete, scan_context_id,
                diagnostics_json, reasons_json
            ) VALUES (
                '2026-06-20T12:00:00+00:00', '2026-06-20', ?, '60 to 61',
                'BUY_YES', 'YES', 1, .72, .64, .24, .26, .02, .01, .27, .45,
                .37, .04, 3, .81, 1.35, 81, 0, ?,
                '{"schema_version": 2, "kind": "trade_decision_signal", "signal": {}}',
                '[]'
            )
            """,
            (ticker, scan_context_id),
        ).lastrowid
    )


def _decision(index: int) -> TradeDecision:
    return TradeDecision(
        ticker=f"KXHIGHTSFO-TEST-B{60 + index}.5",
        label=f"{60 + index} to {61 + index}",
        action="BUY_YES",
        side="YES",
        approved=index == 0,
        signal_approved=index == 0,
        probability=0.72,
        probability_lcb=0.64,
        yes_bid=0.24,
        yes_ask=0.26,
        entry_bid=0.24,
        entry_ask=0.26,
        entry_bid_size=18,
        entry_ask_size=21,
        spread=0.02,
        fee_per_contract=0.01,
        cost_per_contract=0.27,
        edge=0.45,
        edge_lcb=0.37,
        kelly_fraction=0.04,
        recommended_contracts=3.0,
        expected_profit=1.35,
        trade_quality_score=81.0,
        reasons=["passed core edge gate"],
        strike_type="between",
        floor_strike=float(60 + index),
        cap_strike=float(61 + index),
        model_probability=0.76,
        market_probability=0.52,
        binding_constraint="kelly_budget",
    )


def _forecast() -> ForecastSnapshot:
    return ForecastSnapshot(
        target_date=date(2026, 6, 20),
        predicted_high_f=66.0,
        fetched_at="2026-06-20T12:00:00+00:00",
        lead_hours=8.0,
        method="weatheredge-blend",
        google_high_f=66.5,
        nws_high_f=65.5,
        open_meteo_high_f=66.0,
        history_high_f=64.0,
        google_weight=0.35,
        nws_weight=0.35,
        open_meteo_weight=0.2,
        history_weight=0.1,
        station_adjustment_f=-0.25,
        fresh_station_count=4,
        source_count=4,
        raw={"marine_layer_index": 0.7, "ocean_temp_f": 54.0},
    )


def _intraday() -> IntradaySnapshot:
    return IntradaySnapshot(
        target_date=date(2026, 6, 20),
        observed_high_f=64.0,
        latest_temp_f=63.0,
        latest_observed_at="2026-06-20T19:00:00+00:00",
        remaining_forecast_high_f=66.0,
        forecast_fetched_at="2026-06-20T18:45:00+00:00",
        observation_count=12,
        observed_high_source="meteostat",
        is_complete=False,
    )


def _consensus() -> MarketConsensus:
    return MarketConsensus(
        available=True,
        implied_high_f=65.0,
        modal_bin_ticker="KXHIGHTSFO-TEST-B65.5",
        modal_bin_label="65 to 66",
        modal_probability=0.31,
        implied_stdev_f=2.2,
        p10_f=62.0,
        p25_f=64.0,
        median_f=65.0,
        p75_f=67.0,
        p90_f=69.0,
        overround=0.04,
        liquid_bin_count=7,
        bins=(),
    )


def _record(store: PaperStore, decisions: list[TradeDecision]) -> None:
    store.record_decisions(
        "2026-06-20",
        decisions,
        forecast=_forecast(),
        intraday=_intraday(),
        event=pre_resolution_event(decisions),
        market_consensus=_consensus(),
        risk_profile="live",
        bankroll=1234.0,
        strategy_config=StrategyConfig(min_edge=0.02, max_spread=0.07),
    )


def test_record_decisions_creates_one_shared_context_and_links_every_row(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    decisions = [_decision(i) for i in range(12)]

    _record(store, decisions)

    with store.connect() as conn:
        contexts = conn.execute("SELECT * FROM scan_context_snapshots").fetchall()
        station_id = conn.execute(
            "SELECT station_id FROM scan_context_snapshots"
        ).fetchone()[0]
        linked = conn.execute(
            "SELECT scan_context_id, prediction_features_json, diagnostics_json "
            "FROM decision_snapshots ORDER BY id"
        ).fetchall()
    assert len(contexts) == 1
    assert station_id == "KSFO"
    assert len(linked) == len(decisions)
    assert {row[0] for row in linked} == {contexts[0][0]}
    assert all(row[1] is None for row in linked)
    assert all(set(json.loads(row[2])) == {"schema_version", "kind", "signal"} for row in linked)


def test_record_decisions_rolls_back_context_and_rows_on_insert_failure(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_second_decision BEFORE INSERT ON decision_snapshots "
            "WHEN NEW.market_ticker LIKE '%B61.5' BEGIN "
            "SELECT RAISE(ABORT, 'injected decision failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected decision failure"):
        _record(store, [_decision(0), _decision(1)])

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scan_context_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0] == 0


def test_shared_fields_are_not_amplified_in_per_decision_json(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    decisions = [_decision(i) for i in range(20)]
    _record(store, decisions)

    with store.connect() as conn:
        context_bytes = conn.execute(
            "SELECT length(forecast_json) + length(intraday_json) + length(market_json) + "
            "length(market_consensus_json) + length(prediction_features_json) + "
            "length(strategy_config_json) FROM scan_context_snapshots"
        ).fetchone()[0]
        row_bytes = conn.execute(
            "SELECT SUM(length(diagnostics_json)) FROM decision_snapshots"
        ).fetchone()[0]
        payloads = [json.loads(row[0]) for row in conn.execute(
            "SELECT diagnostics_json FROM decision_snapshots"
        )]
    duplicated_shared_bytes = context_bytes * len(decisions)
    normalized_bytes = context_bytes + row_bytes
    assert duplicated_shared_bytes / normalized_bytes >= 9
    assert all("forecast" not in payload and "strategy_config" not in payload for payload in payloads)


def test_order_reconstructs_full_diagnostics_from_context(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    decision = _decision(0)
    _record(store, [decision])

    order_id = store.record_paper_order(
        "2026-06-20",
        decision,
        risk_profile="live",
        strategy_config=StrategyConfig(min_edge=0.02, max_spread=0.07),
    )

    with store.connect() as conn:
        raw = conn.execute("SELECT diagnostics_json FROM paper_orders WHERE id=?", (order_id,)).fetchone()[0]
        created_at = conn.execute(
            "SELECT created_at FROM scan_context_snapshots"
        ).fetchone()[0]
    diagnostics = json.loads(raw)["entry_decision"]["diagnostics"]
    event = pre_resolution_event([decision])
    expected = _decision_diagnostics_payload(
        "2026-06-20",
        decision,
        created_at=created_at,
        forecast=_forecast(),
        intraday=_intraday(),
        event=event,
        market=event.markets[0],
        market_consensus=_consensus(),
        prediction_features=build_prediction_feature_snapshot(
            _forecast(), market_consensus=_consensus(), intraday=_intraday()
        ),
        risk_profile="live",
        bankroll=1234.0,
        strategy_config=StrategyConfig(min_edge=0.02, max_spread=0.07),
        forecast_snapshot_id=None,
        market_snapshot_id=None,
    )
    assert diagnostics == expected
    assert diagnostics["schema_version"] == 1
    assert diagnostics["kind"] == "trade_decision"
    assert diagnostics["signal"]["binding_constraint"] == "kelly_budget"
    assert diagnostics["forecast"]["source_count"] == 4
    assert diagnostics["intraday"]["observed_high_f"] == 64.0
    assert diagnostics["market_consensus"]["implied_high_f"] == 65.0
    assert diagnostics["prediction_features"]["marine_layer_index"] == 0.7
    assert diagnostics["strategy_config"]["min_edge"] == 0.02


def test_legacy_embedded_diagnostics_remain_readable(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    decision = _decision(0)
    legacy = {
        "schema_version": 1,
        "kind": "trade_decision",
        "signal": {"binding_constraint": "legacy"},
        "forecast": {"source_count": 3},
    }
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO decision_snapshots (
                created_at, target_date, market_ticker, label, action, side,
                approved, probability, probability_lcb, yes_bid, yes_ask, spread,
                fee_per_contract, cost_per_contract, edge, edge_lcb, kelly_fraction,
                recommended_contracts, recommended_spend, expected_profit,
                trade_quality_score, intraday_is_complete, diagnostics_json, reasons_json
            ) VALUES (
                '2026-06-20T12:00:00+00:00', '2026-06-20', ?, ?, ?, 'YES',
                1, .72, .64, .24, .26, .02, .01, .27, .45, .37, .04,
                3, .81, 1.35, 81, 0, ?, '[]'
            )
            """,
            (decision.ticker, decision.label, decision.action, json.dumps(legacy)),
        )

    order_id = store.record_paper_order("2026-06-20", decision, risk_profile="live")
    with store.connect() as conn:
        raw = conn.execute("SELECT diagnostics_json FROM paper_orders WHERE id=?", (order_id,)).fetchone()[0]
    diagnostics = json.loads(raw)["entry_decision"]["diagnostics"]
    assert diagnostics == legacy


def test_reconstruction_omits_optional_context_that_was_not_recorded(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    decision = _decision(0)
    store.record_decisions("2026-06-20", [decision], risk_profile="live")

    order_id = store.record_paper_order("2026-06-20", decision, risk_profile="live")

    with store.connect() as conn:
        raw = conn.execute("SELECT diagnostics_json FROM paper_orders WHERE id=?", (order_id,)).fetchone()[0]
    diagnostics = json.loads(raw)["entry_decision"]["diagnostics"]
    assert "forecast" not in diagnostics
    assert "intraday" not in diagnostics
    assert "market_consensus" not in diagnostics
    assert "strategy_config" not in diagnostics


def test_init_concurrently_migrates_legacy_decision_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE decision_snapshots (id INTEGER PRIMARY KEY, created_at TEXT, "
            "target_date TEXT, market_ticker TEXT, label TEXT, action TEXT, side TEXT, "
            "approved INTEGER, probability REAL, probability_lcb REAL, yes_bid REAL, "
            "yes_ask REAL, spread REAL, fee_per_contract REAL, cost_per_contract REAL, "
            "edge REAL, edge_lcb REAL, kelly_fraction REAL, recommended_contracts REAL, "
            "recommended_spend REAL, expected_profit REAL, trade_quality_score REAL, "
            "intraday_is_complete INTEGER DEFAULT 0, reasons_json TEXT)"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: PaperStore(db_path), range(4)))

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_snapshots)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(decision_snapshots)")}
    assert "scan_context_id" in columns
    assert "idx_decision_snapshots_scan_context" in indexes


def test_scan_context_index_is_partial_and_skips_legacy_null_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    store = PaperStore(db_path)
    with store.connect() as conn:
        conn.executemany(
            """
            INSERT INTO decision_snapshots (
                created_at, target_date, market_ticker, label, action, side,
                approved, probability, probability_lcb, yes_bid, yes_ask, spread,
                fee_per_contract, cost_per_contract, edge, edge_lcb, kelly_fraction,
                recommended_contracts, recommended_spend, expected_profit,
                trade_quality_score, intraday_is_complete, reasons_json
            ) VALUES (
                '2026-06-20T12:00:00+00:00', '2026-06-20', ?, 'legacy',
                'BUY_YES', 'YES', 0, .5, .4, .2, .3, .1, .01, .31, .1, .05,
                .01, 0, 0, 0, 0, 0, '[]'
            )
            """,
            [(f"LEGACY-{i}",) for i in range(500)],
        )

    PaperStore(db_path)

    with sqlite3.connect(db_path) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_decision_snapshots_scan_context'"
        ).fetchone()[0]
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM decision_snapshots "
            "WHERE scan_context_id = 42"
        ).fetchall()
        prune_plan = conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM scan_context_snapshots AS c "
            "WHERE NOT EXISTS (SELECT 1 FROM decision_snapshots AS d "
            "WHERE d.scan_context_id=c.id)"
        ).fetchall()
        indexed_rows = conn.execute(
            "SELECT COUNT(*) FROM decision_snapshots "
            "INDEXED BY idx_decision_snapshots_scan_context "
            "WHERE scan_context_id IS NOT NULL"
        ).fetchone()[0]
    assert "WHERE scan_context_id IS NOT NULL" in sql
    assert any("idx_decision_snapshots_scan_context" in str(row) for row in plan)
    assert any(
        "idx_decision_snapshots_scan_context" in str(row) for row in prune_plan
    )
    assert indexed_rows == 0


def test_every_paper_store_connection_enforces_foreign_keys(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")

    with store.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert_decision_with_context(
                conn,
                ticker="ORPHAN-WRITE",
                scan_context_id=999,
            )


def test_init_reports_but_preserves_legacy_foreign_key_violations(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path = tmp_path / "legacy-orphan.db"
    PaperStore(db_path)
    with sqlite3.connect(db_path) as conn:
        orphan_id = _insert_decision_with_context(
            conn,
            ticker="LEGACY-ORPHAN",
            scan_context_id=999,
        )

    with caplog.at_level(logging.ERROR, logger="sfo_kalshi_quant.db"):
        store = PaperStore(db_path)

    assert "foreign key integrity violation" in caplog.text.lower()
    assert "decision_snapshots" in caplog.text
    assert store.foreign_key_violations() == [
        {
            "table": "decision_snapshots",
            "rowid": orphan_id,
            "parent": "scan_context_snapshots",
            "foreign_key_id": 0,
        }
    ]
    with store.connect() as conn:
        assert conn.execute(
            "SELECT scan_context_id FROM decision_snapshots WHERE id=?",
            (orphan_id,),
        ).fetchone()[0] == 999


def test_corrupt_legacy_context_reference_is_visible_on_read(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-orphan.db"
    store = PaperStore(db_path)
    decision = _decision(0)
    with sqlite3.connect(db_path) as conn:
        _insert_decision_with_context(
            conn,
            ticker=decision.ticker,
            scan_context_id=999,
        )

    with pytest.raises(RuntimeError, match="missing scan context 999"):
        store.record_paper_order("2026-06-20", decision, risk_profile="live")


def test_unsupported_scan_context_schema_is_visible_on_read(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    decision = _decision(0)
    _record(store, [decision])
    with store.connect() as conn:
        conn.execute("UPDATE scan_context_snapshots SET schema_version=99")

    with pytest.raises(RuntimeError, match="unsupported scan context schema_version 99"):
        store.record_paper_order("2026-06-20", decision, risk_profile="live")


def test_partial_legacy_scan_context_migrates_to_actionable_read_error(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "partial-context.db"
    PaperStore(db_path)
    decision = _decision(0)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE scan_context_snapshots")
        conn.execute("CREATE TABLE scan_context_snapshots (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO scan_context_snapshots VALUES (7)")
        _insert_decision_with_context(
            conn,
            ticker=decision.ticker,
            scan_context_id=7,
        )

    store = PaperStore(db_path)

    with pytest.raises(RuntimeError, match="partial scan context 7"):
        store.record_paper_order("2026-06-20", decision, risk_profile="live")
