from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import import_module, util as import_util
from pathlib import Path


def _shared_util():
    assert import_util.find_spec("sfo_kalshi_quant._util") is not None
    return import_module("sfo_kalshi_quant._util")


def test_shared_json_and_row_coercion_contract() -> None:
    util = _shared_util()
    assert util._json_object('{"ok": 1}') == {"ok": 1}
    assert util._json_object('["not", "an", "object"]') == {}
    assert util._json_object("not-json") == {}
    assert util._json_list('[1, "two"]') == ["1", "two"]
    assert util._json_list('{"not": "a list"}') == []
    assert util._optional_float("3.5") == 3.5
    assert util._optional_float("bad") is None


def test_shared_sqlite_and_timestamp_helpers() -> None:
    util = _shared_util()
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE present (id INTEGER)")
        assert util._table_exists(conn, "present") is True
        assert util._table_exists(conn, "missing") is False

    assert util._parse_timestamp("2026-07-11T12:30:00Z") == datetime(
        2026, 7, 11, 12, 30, tzinfo=UTC
    )
    assert util._parse_timestamp("bad") is None


def test_shared_private_helpers_each_have_one_definition() -> None:
    package = Path(__file__).resolve().parents[1] / "sfo_kalshi_quant"
    sources = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    assert sources.count("def _table_exists(") == 1
    assert sources.count("def _json_object(") == 1
