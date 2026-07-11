"""Small dependency-free coercion helpers shared across the trading package."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _row_value(row: object, key: str, default: Any = None, *, default_on_none: bool = False):
    try:
        value = row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return default
    if default_on_none and value is None:
        return default
    return value


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_object(value: object) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round_number(value: object) -> float | int | None:
    number = _optional_float(value)
    if number is None:
        return None
    rounded = round(number, 6)
    if rounded.is_integer() and isinstance(value, int):
        return int(rounded)
    return rounded


def _json_safe_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return _round_number(value)
    return str(value)


def _drop_none(value: object):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            cleaned_item = _drop_none(item)
            if cleaned_item is not None:
                cleaned[key] = cleaned_item
        return cleaned
    if isinstance(value, list):
        cleaned = []
        for item in value:
            cleaned_item = _drop_none(item)
            if cleaned_item is not None:
                cleaned.append(cleaned_item)
        return cleaned
    return value
