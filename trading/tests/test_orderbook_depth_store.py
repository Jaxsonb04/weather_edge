"""PaperStore.record_orderbook_depth: persistence for the ladder-depth telemetry."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sfo_kalshi_quant.db import PaperStore


def test_record_orderbook_depth_persists_levels_and_metadata(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "orderbook.db")

    row_id = store.record_orderbook_depth(
        target_date="2026-07-28",
        market_ticker="KXHIGHTSFO-26JUL28-T78",
        yes_levels=[[0.01, 522.0]],
        no_levels=[[0.93, 11.0], [0.97, 745.0]],
        levels_requested=5,
        scan_run_id="scan-abc",
        risk_profile="research",
    )
    assert isinstance(row_id, int) and row_id > 0

    with sqlite3.connect(tmp_path / "orderbook.db") as conn:
        row = conn.execute(
            "SELECT target_date, market_ticker, risk_profile, scan_run_id, "
            "levels_requested, yes_levels_json, no_levels_json "
            "FROM market_orderbook_depth_snapshots WHERE id=?",
            (row_id,),
        ).fetchone()

    assert row is not None
    (
        target_date,
        market_ticker,
        risk_profile,
        scan_run_id,
        levels_requested,
        yes_json,
        no_json,
    ) = row
    assert target_date == "2026-07-28"
    assert market_ticker == "KXHIGHTSFO-26JUL28-T78"
    assert risk_profile == "research"
    assert scan_run_id == "scan-abc"
    assert levels_requested == 5
    assert json.loads(yes_json) == [[0.01, 522.0]]
    assert json.loads(no_json) == [[0.93, 11.0], [0.97, 745.0]]


def test_record_orderbook_depth_allows_optional_fields_to_be_absent(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "orderbook2.db")

    row_id = store.record_orderbook_depth(
        target_date="2026-07-28",
        market_ticker="KXHIGHCHI-26JUL28-T90",
        yes_levels=[],
        no_levels=[],
        levels_requested=3,
    )

    with sqlite3.connect(tmp_path / "orderbook2.db") as conn:
        scan_run_id, risk_profile = conn.execute(
            "SELECT scan_run_id, risk_profile FROM market_orderbook_depth_snapshots WHERE id=?",
            (row_id,),
        ).fetchone()
    assert scan_run_id is None
    assert risk_profile is None


def test_multiple_snapshots_for_the_same_market_are_independent_rows(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "orderbook3.db")

    first = store.record_orderbook_depth(
        target_date="2026-07-28",
        market_ticker="KXHIGHTSFO-26JUL28-T78",
        yes_levels=[[0.01, 10.0]],
        no_levels=[[0.97, 5.0]],
        levels_requested=3,
    )
    second = store.record_orderbook_depth(
        target_date="2026-07-28",
        market_ticker="KXHIGHTSFO-26JUL28-T78",
        yes_levels=[[0.02, 8.0]],
        no_levels=[[0.96, 6.0]],
        levels_requested=3,
    )

    assert first != second
    with sqlite3.connect(tmp_path / "orderbook3.db") as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM market_orderbook_depth_snapshots WHERE market_ticker=?",
            ("KXHIGHTSFO-26JUL28-T78",),
        ).fetchone()[0]
    assert count == 2
