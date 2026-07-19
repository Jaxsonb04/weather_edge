"""Google Weather event accounting and short-lived parsed runtime storage.

Only request metadata needed to enforce the event budget belongs in the
permanent weather database. Parsed temperatures live separately in the
TTL-enforced runtime database; raw responses and request secrets live in
neither schema.
"""

from __future__ import annotations

import math
import os
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from cities import CITY_BY_SLUG
from weather_cache_config import (
    GOOGLE_CURRENT_TTL,
    GOOGLE_FUTURE_DAILY_TTL,
    GOOGLE_HOURLY_TTL,
    GOOGLE_TODAY_DAILY_TTL,
    GOOGLE_WEATHER_DAILY_EVENT_BUDGET,
    GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET,
    GOOGLE_WEATHER_SOFT_MONTHLY_CEILING,
)


PACIFIC = ZoneInfo("America/Los_Angeles")
GOOGLE_RUNTIME_ROOT = Path("/run/weatheredge")
GOOGLE_USAGE_ENDPOINTS = frozenset({"hourly", "daily", "current"})
GOOGLE_USAGE_ERROR_KINDS = frozenset(
    {"timeout", "transport", "http", "parse", "unknown"}
)


class GoogleWeatherBudgetExceeded(RuntimeError):
    """A reservation would exceed an enforced Google Weather event limit."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(f"Google Weather {scope} event budget reached")


class GoogleUsageLifecycleError(RuntimeError):
    """A billing event was asked to make an invalid state transition."""


@dataclass(frozen=True)
class GoogleUsageEvent:
    reservation_id: str
    endpoint: str
    page_number: int


@dataclass(frozen=True)
class GoogleUsageEventState:
    reservation_id: str
    endpoint: str
    page_number: int
    status: str
    billable_events: int
    dispatched_at: str | None
    completed_at: str | None
    response_status_class: int | None
    error_kind: str | None


@dataclass(frozen=True)
class GoogleUsageCounts:
    daily_events: int
    monthly_events: int


@dataclass(frozen=True)
class GoogleHourlyRuntime:
    city_slug: str
    station_id: str
    issued_at: datetime
    valid_at: datetime
    temperature_f: float
    expires_at: datetime


@dataclass(frozen=True)
class GoogleDailyRuntime:
    city_slug: str
    station_id: str
    issued_at: datetime
    target_date: str
    high_f: float
    expires_at: datetime


@dataclass(frozen=True)
class GoogleCurrentRuntime:
    city_slug: str
    station_id: str
    issued_at: datetime
    observed_at: datetime
    temperature_f: float
    expires_at: datetime


@dataclass(frozen=True)
class GoogleRuntimeHigh:
    city_slug: str
    station_id: str
    issued_at: datetime
    target_date: str
    high_f: float
    covered_hours: int
    complete: bool
    expires_at: datetime


def assert_runtime_path(path: Path, *, production: bool) -> None:
    """Keep TTL-bound content in an app-owned, non-symlinked tmpfs tree."""

    if not production:
        return
    root = Path(os.path.abspath(GOOGLE_RUNTIME_ROOT))
    candidate = Path(os.path.abspath(path))
    if candidate == root or not candidate.is_relative_to(root):
        raise RuntimeError("Google runtime content must live under /run/weatheredge")
    directories = [root]
    relative_parent = candidate.parent.relative_to(root)
    current = root
    for part in relative_parent.parts:
        current /= part
        directories.append(current)
    for directory in directories:
        try:
            metadata = os.lstat(directory)
        except FileNotFoundError:
            raise RuntimeError(
                "Google runtime production directory must already exist"
            ) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(
                "Google runtime production directory must not be a symlink"
            )
        if metadata.st_uid != os.geteuid() or metadata.st_mode & (
            stat.S_IWGRP | stat.S_IWOTH
        ):
            raise RuntimeError(
                "Google runtime production directory must be app-owned and protected"
            )
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("Google runtime database must not be a symlink")
    if metadata.st_uid != os.geteuid() or metadata.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        raise RuntimeError(
            "Google runtime database must be app-owned and protected"
        )


def _runtime_file_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    return metadata.st_dev, metadata.st_ino


_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS google_hourly_runtime (
    city_slug TEXT NOT NULL,
    station_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    temperature_f REAL NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(city_slug, station_id, issued_at, valid_at)
);
CREATE INDEX IF NOT EXISTS idx_google_hourly_runtime_expiry
    ON google_hourly_runtime(expires_at);
CREATE TABLE IF NOT EXISTS google_daily_runtime (
    city_slug TEXT NOT NULL,
    station_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    high_f REAL NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(city_slug, station_id, issued_at, target_date)
);
CREATE INDEX IF NOT EXISTS idx_google_daily_runtime_expiry
    ON google_daily_runtime(expires_at);
CREATE TABLE IF NOT EXISTS google_current_runtime (
    city_slug TEXT NOT NULL,
    station_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    temperature_f REAL NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(city_slug, station_id, issued_at, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_google_current_runtime_expiry
    ON google_current_runtime(expires_at);
CREATE TABLE IF NOT EXISTS google_runtime_high (
    city_slug TEXT NOT NULL,
    station_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    high_f REAL NOT NULL,
    covered_hours INTEGER NOT NULL CHECK(covered_hours BETWEEN 1 AND 24),
    complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
    expires_at TEXT NOT NULL,
    CHECK(complete = CASE WHEN covered_hours = 24 THEN 1 ELSE 0 END),
    PRIMARY KEY(city_slug, station_id, issued_at, target_date)
);
CREATE INDEX IF NOT EXISTS idx_google_runtime_high_expiry
    ON google_runtime_high(expires_at);
CREATE TABLE IF NOT EXISTS google_runtime_generation_watermarks (
    content_table TEXT NOT NULL CHECK(content_table IN (
        'google_hourly_runtime',
        'google_daily_runtime',
        'google_current_runtime',
        'google_runtime_high'
    )),
    city_slug TEXT NOT NULL,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    newest_issued_at TEXT NOT NULL,
    PRIMARY KEY(content_table, city_slug, station_id, target_date)
);
CREATE TABLE IF NOT EXISTS google_runtime_schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL
);
"""

_RUNTIME_SCHEMA_VERSION = 2
_RUNTIME_OWNED_TABLES = (
    "google_hourly_runtime",
    "google_daily_runtime",
    "google_current_runtime",
    "google_runtime_high",
    "google_runtime_generation_watermarks",
    "google_runtime_schema_metadata",
)
_RUNTIME_SCHEMA_COLUMNS = {
    "google_hourly_runtime": {
        "city_slug",
        "station_id",
        "issued_at",
        "valid_at",
        "temperature_f",
        "expires_at",
    },
    "google_daily_runtime": {
        "city_slug",
        "station_id",
        "issued_at",
        "target_date",
        "high_f",
        "expires_at",
    },
    "google_current_runtime": {
        "city_slug",
        "station_id",
        "issued_at",
        "observed_at",
        "temperature_f",
        "expires_at",
    },
    "google_runtime_high": {
        "city_slug",
        "station_id",
        "issued_at",
        "target_date",
        "high_f",
        "covered_hours",
        "complete",
        "expires_at",
    },
    "google_runtime_generation_watermarks": {
        "content_table",
        "city_slug",
        "station_id",
        "target_date",
        "newest_issued_at",
    },
    "google_runtime_schema_metadata": {"singleton", "schema_version"},
}
_RUNTIME_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in _RUNTIME_SCHEMA.split(";") if statement.strip()
)

_RUNTIME_GENERATION_SCOPES = {
    "google_hourly_runtime": ("city_slug", "station_id"),
    "google_daily_runtime": ("city_slug", "station_id"),
    "google_current_runtime": ("city_slug", "station_id"),
    "google_runtime_high": ("city_slug", "station_id", "target_date"),
}


_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS google_weather_usage_events (
    id INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    billing_month TEXT NOT NULL,
    billing_date_pacific TEXT NOT NULL,
    city_slug TEXT NOT NULL,
    station_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    reserved_at TEXT NOT NULL,
    dispatched_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('reserved','consumed','success','cancelled')),
    billable_events INTEGER NOT NULL CHECK(billable_events IN (0,1)),
    response_status_class INTEGER,
    error_kind TEXT,
    UNIQUE(reservation_id, endpoint, page_number)
);
CREATE INDEX IF NOT EXISTS idx_google_weather_usage_billing_date
    ON google_weather_usage_events(billing_date_pacific, status);
CREATE INDEX IF NOT EXISTS idx_google_weather_usage_billing_month
    ON google_weather_usage_events(billing_month, status);
"""


def _aware_utc(value: datetime | None) -> datetime:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Google usage timestamps must be timezone-aware")
    return instant.astimezone(timezone.utc)


def _utc_text(value: datetime | None) -> str:
    return _aware_utc(value).isoformat(timespec="microseconds")


def _canonical_city_station(
    city_slug: object, station_id: object
) -> tuple[str, str]:
    city = CITY_BY_SLUG.get(city_slug) if type(city_slug) is str else None
    if city is None:
        raise ValueError("city_slug must be a configured canonical city")
    if type(station_id) is not str or station_id != city.nws_station_id:
        raise ValueError("city and station must match the configured canonical pair")
    return city_slug, station_id


def _endpoint(value: object) -> str:
    if type(value) is not str or value not in GOOGLE_USAGE_ENDPOINTS:
        raise ValueError("endpoint must be a supported Google Weather endpoint")
    return value


def _reservation_id(value: object | None) -> str:
    if value is None:
        return uuid.uuid4().hex
    if type(value) is not str:
        raise ValueError("reservation_id must be a canonical version-4 UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        raise ValueError("reservation_id must be a canonical version-4 UUID") from None
    if (
        value != parsed.hex
        or parsed.version != 4
        or parsed.variant != uuid.RFC_4122
    ):
        raise ValueError("reservation_id must be a canonical version-4 UUID")
    return value


def _error_kind(value: object) -> str:
    if type(value) is not str or value not in GOOGLE_USAGE_ERROR_KINDS:
        raise ValueError("error_kind must be a supported failure category")
    return value


def _finite_float(name: str, value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _positive_finite_timeout(value: object) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timeout_seconds must be finite and positive") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    return timeout


def _datetime_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _date_text(name: str, value: object) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO date") from None
    if value != parsed.isoformat():
        raise ValueError(f"{name} must be an ISO date")
    return value


def _runtime_schema_is_current(connection: sqlite3.Connection) -> bool:
    for table, expected_columns in _RUNTIME_SCHEMA_COLUMNS.items():
        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if columns != expected_columns:
            return False
    version_row = connection.execute(
        """
        SELECT schema_version
        FROM google_runtime_schema_metadata
        WHERE singleton = 1
        """
    ).fetchone()
    if version_row is None or int(version_row[0]) != _RUNTIME_SCHEMA_VERSION:
        return False
    high_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("google_runtime_high",),
    ).fetchone()
    if high_sql_row is None or high_sql_row[0] is None:
        return False
    normalized_high_sql = "".join(str(high_sql_row[0]).lower().split())
    return (
        "check(covered_hoursbetween1and24)" in normalized_high_sql
        and "check(complete=casewhencovered_hours=24then1else0end)"
        in normalized_high_sql
    )


def _initialize_runtime_schema(connection: sqlite3.Connection) -> None:
    """Install or transactionally replace only the disposable runtime schema."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        schema_is_current = _runtime_schema_is_current(connection)
        if not schema_is_current:
            for table in reversed(_RUNTIME_OWNED_TABLES):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in _RUNTIME_SCHEMA_STATEMENTS:
            connection.execute(statement)
        if not schema_is_current:
            connection.execute(
                """
                INSERT INTO google_runtime_schema_metadata (
                    singleton, schema_version
                ) VALUES (1, ?)
                """,
                (_RUNTIME_SCHEMA_VERSION,),
            )
            if not _runtime_schema_is_current(connection):
                raise RuntimeError("Google runtime schema validation failed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


class GoogleRuntimeStore:
    """TTL-enforced storage for parsed Google Weather runtime content."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        production: bool,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.production = production
        self._runtime_identity: tuple[int, int] | None = None
        assert_runtime_path(self.db_path, production=production)
        self.timeout_seconds = _positive_finite_timeout(timeout_seconds)
        if not production:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            _initialize_runtime_schema(connection)
        if production:
            os.chmod(self.db_path, 0o600, follow_symlinks=False)
            assert_runtime_path(self.db_path, production=True)
            self._runtime_identity = _runtime_file_identity(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        before: tuple[int, int] | None = None
        if self.production:
            assert_runtime_path(self.db_path, production=True)
            if self._runtime_identity is not None and not os.path.lexists(
                self.db_path
            ):
                raise RuntimeError("Google runtime database changed identity")
            if os.path.lexists(self.db_path):
                before = _runtime_file_identity(self.db_path)
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}"
            )
            if self.production:
                assert_runtime_path(self.db_path, production=True)
                after = _runtime_file_identity(self.db_path)
                if before is not None and before != after:
                    raise RuntimeError("Google runtime database changed identity")
                if (
                    self._runtime_identity is not None
                    and self._runtime_identity != after
                ):
                    raise RuntimeError("Google runtime database changed identity")
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open one runtime connection and deterministically release its handle."""

        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _write_latest_generation(
        self,
        *,
        table: str,
        scope_values: tuple[str, ...],
        issued_at: str,
        insert_sql: str,
        insert_values: tuple[object, ...],
    ) -> bool:
        return self._store_generation(
            table=table,
            scope_values=scope_values,
            issued_at=issued_at,
            insert_sql=insert_sql,
            insert_rows=(insert_values,),
            replace_scope=False,
        )

    def _store_generation(
        self,
        *,
        table: str,
        scope_values: tuple[str, ...],
        issued_at: str,
        insert_sql: str,
        insert_rows: tuple[tuple[object, ...], ...],
        replace_scope: bool,
    ) -> bool:
        """Atomically retain or replace the newest generation for one scope."""

        scope_columns = _RUNTIME_GENERATION_SCOPES.get(table)
        if scope_columns is None or len(scope_columns) != len(scope_values):
            raise ValueError("unsupported Google runtime generation scope")
        where = " AND ".join(f"{column} = ?" for column in scope_columns)
        city_slug, station_id = scope_values[:2]
        target_date = scope_values[2] if len(scope_values) == 3 else ""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            watermark_row = connection.execute(
                """
                SELECT newest_issued_at
                FROM google_runtime_generation_watermarks
                WHERE content_table = ? AND city_slug = ? AND station_id = ?
                  AND target_date = ?
                """,
                (table, city_slug, station_id, target_date),
            ).fetchone()
            row = connection.execute(
                f"SELECT MAX(issued_at) AS issued_at FROM {table} WHERE {where}",
                scope_values,
            ).fetchone()
            candidates = (
                watermark_row["newest_issued_at"] if watermark_row is not None else None,
                row["issued_at"] if row is not None else None,
            )
            present_candidates = tuple(
                value for value in candidates if value is not None
            )
            latest = max(present_candidates) if present_candidates else None
            if latest is not None and issued_at < latest:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO google_runtime_generation_watermarks (
                    content_table, city_slug, station_id, target_date,
                    newest_issued_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_table, city_slug, station_id, target_date)
                DO UPDATE SET newest_issued_at = excluded.newest_issued_at
                """,
                (table, city_slug, station_id, target_date, issued_at),
            )
            if replace_scope:
                connection.execute(
                    f"DELETE FROM {table} WHERE {where}", scope_values
                )
            elif latest is not None and issued_at > latest:
                connection.execute(
                    f"DELETE FROM {table} WHERE {where} AND issued_at < ?",
                    (*scope_values, issued_at),
                )
            connection.executemany(insert_sql, insert_rows)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def replace_hourly_generation(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        temperatures_by_valid_at: Mapping[datetime, float],
        stored_at: datetime | None = None,
    ) -> bool:
        """Atomically publish the complete hourly issue generation for Task 4."""

        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        if not isinstance(temperatures_by_valid_at, Mapping):
            raise ValueError("temperatures_by_valid_at must be a mapping")
        issued = _aware_utc(issued_at)
        stored = _aware_utc(stored_at)
        expires = stored + GOOGLE_HOURLY_TTL
        insert_rows = tuple(
            (
                city_slug,
                station_id,
                _utc_text(issued),
                _utc_text(_aware_utc(valid_at)),
                _finite_float("temperature_f", temperature_f),
                _utc_text(expires),
            )
            for valid_at, temperature_f in temperatures_by_valid_at.items()
        )
        return self._store_generation(
            table="google_hourly_runtime",
            scope_values=(city_slug, station_id),
            issued_at=_utc_text(issued),
            insert_sql="""
                INSERT INTO google_hourly_runtime (
                    city_slug, station_id, issued_at, valid_at,
                    temperature_f, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
            insert_rows=insert_rows,
            replace_scope=True,
        )

    def write_hourly(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        valid_at: datetime,
        temperature_f: float,
        stored_at: datetime | None = None,
    ) -> bool:
        """Publish a complete one-row hourly generation."""

        return self.replace_hourly_generation(
            city_slug=city_slug,
            station_id=station_id,
            issued_at=issued_at,
            temperatures_by_valid_at={valid_at: temperature_f},
            stored_at=stored_at,
        )

    def active_hourly(
        self,
        *,
        city_slug: str,
        station_id: str,
        now: datetime | None = None,
    ) -> tuple[GoogleHourlyRuntime, ...]:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, valid_at,
                       temperature_f, expires_at
                FROM google_hourly_runtime
                WHERE city_slug = ? AND station_id = ?
                  AND issued_at = (
                      SELECT MAX(issued_at)
                      FROM google_hourly_runtime
                      WHERE city_slug = ? AND station_id = ?
                  )
                  AND expires_at > ?
                ORDER BY valid_at
                """,
                (
                    city_slug,
                    station_id,
                    city_slug,
                    station_id,
                    _utc_text(now),
                ),
            ).fetchall()
        return tuple(
            GoogleHourlyRuntime(
                city_slug=row["city_slug"],
                station_id=row["station_id"],
                issued_at=_datetime_from_text(row["issued_at"]),
                valid_at=_datetime_from_text(row["valid_at"]),
                temperature_f=float(row["temperature_f"]),
                expires_at=_datetime_from_text(row["expires_at"]),
            )
            for row in rows
        )

    def write_daily(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        target_date: str,
        high_f: float,
        stored_at: datetime | None = None,
    ) -> bool:
        """Publish a complete one-row daily generation."""

        return self.replace_daily_generation(
            city_slug=city_slug,
            station_id=station_id,
            issued_at=issued_at,
            highs_by_target_date={target_date: high_f},
            stored_at=stored_at,
        )

    def replace_daily_generation(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        highs_by_target_date: Mapping[str, float],
        stored_at: datetime | None = None,
    ) -> bool:
        """Atomically publish the complete daily issue generation for Task 4."""

        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        if not isinstance(highs_by_target_date, Mapping):
            raise ValueError("highs_by_target_date must be a mapping")
        city = CITY_BY_SLUG[city_slug]
        issued = _aware_utc(issued_at)
        stored = _aware_utc(stored_at)
        local_today = stored.astimezone(ZoneInfo(city.civil_tz_name)).date()
        insert_rows: list[tuple[object, ...]] = []
        for target_date, high_f in highs_by_target_date.items():
            target = _date_text("target_date", target_date)
            ttl = (
                GOOGLE_TODAY_DAILY_TTL
                if date.fromisoformat(target) <= local_today
                else GOOGLE_FUTURE_DAILY_TTL
            )
            insert_rows.append(
                (
                    city_slug,
                    station_id,
                    _utc_text(issued),
                    target,
                    _finite_float("high_f", high_f),
                    _utc_text(stored + ttl),
                )
            )
        return self._store_generation(
            table="google_daily_runtime",
            scope_values=(city_slug, station_id),
            issued_at=_utc_text(issued),
            insert_sql="""
                INSERT INTO google_daily_runtime (
                    city_slug, station_id, issued_at, target_date,
                    high_f, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
            insert_rows=tuple(insert_rows),
            replace_scope=True,
        )

    def active_daily(
        self,
        *,
        city_slug: str,
        station_id: str,
        now: datetime | None = None,
    ) -> tuple[GoogleDailyRuntime, ...]:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, target_date,
                       high_f, expires_at
                FROM google_daily_runtime
                WHERE city_slug = ? AND station_id = ?
                  AND issued_at = (
                      SELECT MAX(issued_at)
                      FROM google_daily_runtime
                      WHERE city_slug = ? AND station_id = ?
                  )
                  AND expires_at > ?
                ORDER BY target_date
                """,
                (
                    city_slug,
                    station_id,
                    city_slug,
                    station_id,
                    _utc_text(now),
                ),
            ).fetchall()
        return tuple(
            GoogleDailyRuntime(
                city_slug=row["city_slug"],
                station_id=row["station_id"],
                issued_at=_datetime_from_text(row["issued_at"]),
                target_date=row["target_date"],
                high_f=float(row["high_f"]),
                expires_at=_datetime_from_text(row["expires_at"]),
            )
            for row in rows
        )

    def write_current(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        observed_at: datetime,
        temperature_f: float,
        stored_at: datetime | None = None,
    ) -> bool:
        """Publish the complete one-row current-condition generation."""

        return self.replace_current_generation(
            city_slug=city_slug,
            station_id=station_id,
            issued_at=issued_at,
            observed_at=observed_at,
            temperature_f=temperature_f,
            stored_at=stored_at,
        )

    def replace_current_generation(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        observed_at: datetime,
        temperature_f: float,
        stored_at: datetime | None = None,
    ) -> bool:
        """Atomically publish the current-condition issue generation for Task 4."""

        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        issued = _aware_utc(issued_at)
        observed = _aware_utc(observed_at)
        stored = _aware_utc(stored_at)
        return self._store_generation(
            table="google_current_runtime",
            scope_values=(city_slug, station_id),
            issued_at=_utc_text(issued),
            insert_sql="""
                INSERT INTO google_current_runtime (
                    city_slug, station_id, issued_at, observed_at,
                    temperature_f, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
            insert_rows=(
                (
                    city_slug,
                    station_id,
                    _utc_text(issued),
                    _utc_text(observed),
                    _finite_float("temperature_f", temperature_f),
                    _utc_text(stored + GOOGLE_CURRENT_TTL),
                ),
            ),
            replace_scope=True,
        )

    def active_current(
        self,
        *,
        city_slug: str,
        station_id: str,
        now: datetime | None = None,
    ) -> GoogleCurrentRuntime | None:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, observed_at,
                       temperature_f, expires_at
                FROM google_current_runtime
                WHERE city_slug = ? AND station_id = ?
                  AND issued_at = (
                      SELECT MAX(issued_at)
                      FROM google_current_runtime
                      WHERE city_slug = ? AND station_id = ?
                  )
                  AND expires_at > ?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (
                    city_slug,
                    station_id,
                    city_slug,
                    station_id,
                    _utc_text(now),
                ),
            ).fetchone()
        if row is None:
            return None
        return GoogleCurrentRuntime(
            city_slug=row["city_slug"],
            station_id=row["station_id"],
            issued_at=_datetime_from_text(row["issued_at"]),
            observed_at=_datetime_from_text(row["observed_at"]),
            temperature_f=float(row["temperature_f"]),
            expires_at=_datetime_from_text(row["expires_at"]),
        )

    def write_runtime_high(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        target_date: str,
        constituents: tuple[GoogleHourlyRuntime, ...],
    ) -> bool:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        city = CITY_BY_SLUG[city_slug]
        issued = _aware_utc(issued_at)
        target = _date_text("target_date", target_date)
        if type(constituents) is not tuple or not constituents:
            raise ValueError("constituents must be a non-empty tuple")
        valid_times: set[datetime] = set()
        temperatures: list[float] = []
        expiries: list[datetime] = []
        standard_tz = city.fixed_standard_timezone()
        for row in constituents:
            if type(row) is not GoogleHourlyRuntime:
                raise ValueError("constituents must be Google hourly runtime rows")
            if (
                row.city_slug != city_slug
                or row.station_id != station_id
                or _aware_utc(row.issued_at) != issued
            ):
                raise ValueError("constituent identity must match the runtime high")
            valid = _aware_utc(row.valid_at)
            local_valid = valid.astimezone(standard_tz)
            if (
                local_valid.date().isoformat() != target
                or local_valid.minute != 0
                or local_valid.second != 0
                or local_valid.microsecond != 0
            ):
                raise ValueError(
                    "constituents must be exact hours in the target station day"
                )
            if valid in valid_times:
                raise ValueError("constituent valid times must be unique")
            valid_times.add(valid)
            temperatures.append(_finite_float("temperature_f", row.temperature_f))
            # A page of distinct forecast hours normally shares one expiry.
            # The valid timestamp is the constituent identity; expiry is TTL.
            expiries.append(_aware_utc(row.expires_at))
        covered_hours = len(valid_times)
        if covered_hours > 24:
            raise ValueError("constituents cannot exceed a 24-hour station day")
        complete = covered_hours == 24
        high = max(temperatures)
        expires = min(expiries)
        return self._write_latest_generation(
            table="google_runtime_high",
            scope_values=(city_slug, station_id, target),
            issued_at=_utc_text(issued),
            insert_sql="""
                INSERT INTO google_runtime_high (
                    city_slug, station_id, issued_at, target_date, high_f,
                    covered_hours, complete, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(city_slug, station_id, issued_at, target_date)
                DO UPDATE SET
                    high_f = excluded.high_f,
                    covered_hours = excluded.covered_hours,
                    complete = excluded.complete,
                    expires_at = excluded.expires_at
                """,
            insert_values=(
                city_slug,
                station_id,
                _utc_text(issued),
                target,
                high,
                covered_hours,
                int(complete),
                _utc_text(expires),
            ),
        )

    def active_runtime_high(
        self,
        *,
        city_slug: str,
        station_id: str,
        target_date: str,
        now: datetime | None = None,
    ) -> GoogleRuntimeHigh | None:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        target = _date_text("target_date", target_date)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, target_date, high_f,
                       covered_hours, complete, expires_at
                FROM google_runtime_high
                WHERE city_slug = ? AND station_id = ? AND target_date = ?
                  AND issued_at = (
                      SELECT MAX(issued_at)
                      FROM google_runtime_high
                      WHERE city_slug = ? AND station_id = ? AND target_date = ?
                  )
                  AND expires_at > ?
                LIMIT 1
                """,
                (
                    city_slug,
                    station_id,
                    target,
                    city_slug,
                    station_id,
                    target,
                    _utc_text(now),
                ),
            ).fetchone()
        if row is None:
            return None
        return GoogleRuntimeHigh(
            city_slug=row["city_slug"],
            station_id=row["station_id"],
            issued_at=_datetime_from_text(row["issued_at"]),
            target_date=row["target_date"],
            high_f=float(row["high_f"]),
            covered_hours=int(row["covered_hours"]),
            complete=bool(row["complete"]),
            expires_at=_datetime_from_text(row["expires_at"]),
        )

    def next_expiry(self, *, now: datetime | None = None) -> datetime | None:
        cutoff = _utc_text(now)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT MIN(expires_at) AS expires_at
                FROM (
                    SELECT expires_at FROM google_hourly_runtime
                    UNION ALL
                    SELECT expires_at FROM google_daily_runtime
                    UNION ALL
                    SELECT expires_at FROM google_current_runtime
                    UNION ALL
                    SELECT expires_at FROM google_runtime_high
                )
                WHERE expires_at > ?
                """,
                (cutoff,),
            ).fetchone()
        return (
            _datetime_from_text(row["expires_at"])
            if row is not None and row["expires_at"] is not None
            else None
        )

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Physically delete all expired runtime content in one transaction."""

        cutoff = _utc_text(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = 0
            for table in (
                "google_hourly_runtime",
                "google_daily_runtime",
                "google_current_runtime",
                "google_runtime_high",
            ):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE expires_at <= ?", (cutoff,)
                )
                deleted += cursor.rowcount
            connection.commit()
            return deleted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class GoogleUsageLedger:
    """Reserve and finalize Google Weather billing events atomically."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        daily_budget: int = GOOGLE_WEATHER_DAILY_EVENT_BUDGET,
        monthly_budget: int = GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET,
        soft_monthly_ceiling: int = GOOGLE_WEATHER_SOFT_MONTHLY_CEILING,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.daily_budget = self._budget("daily_budget", daily_budget)
        self.monthly_budget = self._budget("monthly_budget", monthly_budget)
        self.soft_monthly_ceiling = self._budget(
            "soft_monthly_ceiling", soft_monthly_ceiling
        )
        self.timeout_seconds = _positive_finite_timeout(timeout_seconds)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(_USAGE_SCHEMA)

    @staticmethod
    def _budget(name: str, value: object) -> int:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}"
            )
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open one ledger connection and deterministically release its handle."""

        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def reserve_event(
        self,
        *,
        city_slug: str,
        station_id: str,
        endpoint: str,
        page_number: int,
        reservation_id: str | None = None,
        now: datetime | None = None,
    ) -> GoogleUsageEvent:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        endpoint = _endpoint(endpoint)
        if type(page_number) is not int or page_number < 0:
            raise ValueError("page_number must be a non-negative integer")
        reservation_id = _reservation_id(reservation_id)
        instant = _aware_utc(now)
        pacific = instant.astimezone(PACIFIC)
        billing_date = pacific.date().isoformat()
        billing_month = pacific.strftime("%Y-%m")
        handle = GoogleUsageEvent(reservation_id, endpoint, page_number)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT city_slug, station_id
                FROM google_weather_usage_events
                WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                """,
                (reservation_id, endpoint, page_number),
            ).fetchone()
            if existing is not None:
                if (
                    existing["city_slug"] == city_slug
                    and existing["station_id"] == station_id
                ):
                    connection.commit()
                    return handle
                raise GoogleUsageLifecycleError(
                    "Google Weather reservation metadata conflicts with existing event"
                )
            daily_events = self._count(connection, "billing_date_pacific", billing_date)
            monthly_events = self._count(connection, "billing_month", billing_month)
            if daily_events >= self.daily_budget:
                raise GoogleWeatherBudgetExceeded("daily")
            if monthly_events >= self.monthly_budget:
                raise GoogleWeatherBudgetExceeded("monthly")
            if monthly_events >= self.soft_monthly_ceiling:
                raise GoogleWeatherBudgetExceeded("soft monthly")
            connection.execute(
                """
                INSERT INTO google_weather_usage_events (
                    reservation_id, billing_month, billing_date_pacific,
                    city_slug, station_id, endpoint, page_number, reserved_at,
                    status, billable_events
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 1)
                """,
                (
                    reservation_id,
                    billing_month,
                    billing_date,
                    city_slug,
                    station_id,
                    endpoint,
                    page_number,
                    instant.isoformat(timespec="microseconds"),
                ),
            )
            connection.commit()
            return handle
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _count(connection: sqlite3.Connection, column: str, value: str) -> int:
        row = connection.execute(
            f"""
            SELECT COALESCE(SUM(billable_events), 0)
            FROM google_weather_usage_events
            WHERE {column} = ? AND status IN ('reserved', 'consumed', 'success')
            """,
            (value,),
        ).fetchone()
        return int(row[0])

    def cancel_before_dispatch(
        self, event: GoogleUsageEvent, *, now: datetime | None = None
    ) -> bool:
        completed_at = _utc_text(now)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE google_weather_usage_events
                SET status = 'cancelled', billable_events = 0, completed_at = ?
                WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                  AND status = 'reserved' AND dispatched_at IS NULL
                """,
                (
                    completed_at,
                    event.reservation_id,
                    event.endpoint,
                    event.page_number,
                ),
            )
            return cursor.rowcount == 1

    def cancel_stale_undispatched(
        self,
        *,
        before: datetime,
        now: datetime | None = None,
    ) -> int:
        cutoff = _utc_text(before)
        completed_at = _utc_text(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE google_weather_usage_events
                SET status = 'cancelled', billable_events = 0, completed_at = ?
                WHERE status = 'reserved' AND dispatched_at IS NULL
                  AND reserved_at < ?
                """,
                (completed_at, cutoff),
            )
            connection.commit()
            return cursor.rowcount
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_dispatched(
        self, event: GoogleUsageEvent, *, now: datetime | None = None
    ) -> GoogleUsageEventState:
        dispatched_at = _utc_text(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE google_weather_usage_events
                SET status = 'consumed', dispatched_at = ?
                WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                  AND status = 'reserved' AND dispatched_at IS NULL
                """,
                (
                    dispatched_at,
                    event.reservation_id,
                    event.endpoint,
                    event.page_number,
                ),
            )
            if cursor.rowcount == 0:
                status_row = connection.execute(
                    """
                    SELECT status FROM google_weather_usage_events
                    WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                    """,
                    (event.reservation_id, event.endpoint, event.page_number),
                ).fetchone()
                if status_row is None:
                    raise KeyError("unknown Google Weather usage event")
                raise GoogleUsageLifecycleError(
                    "Google Weather event is not dispatchable"
                )
            row = connection.execute(
                """
                SELECT reservation_id, endpoint, page_number, status,
                       billable_events, dispatched_at, completed_at,
                       response_status_class, error_kind
                FROM google_weather_usage_events
                WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                """,
                (event.reservation_id, event.endpoint, event.page_number),
            ).fetchone()
            connection.commit()
            return GoogleUsageEventState(**dict(row))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_event(
        self,
        event: GoogleUsageEvent,
        *,
        success: bool,
        response_status_class: int | None = None,
        error_kind: str | None = None,
        now: datetime | None = None,
    ) -> GoogleUsageEventState:
        if type(success) is not bool:
            raise ValueError("success must be a boolean")
        if response_status_class is not None and (
            type(response_status_class) is not int
            or not 1 <= response_status_class <= 5
        ):
            raise ValueError("response_status_class must be an HTTP class from 1 to 5")
        if success:
            if error_kind is not None:
                raise ValueError("successful events cannot have an error_kind")
            status = "success"
        else:
            error_kind = _error_kind(error_kind)
            status = "consumed"

        completed_at = _utc_text(now)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE google_weather_usage_events
                SET status = ?, completed_at = ?, response_status_class = ?,
                    error_kind = ?
                WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                  AND status = 'consumed' AND dispatched_at IS NOT NULL
                  AND completed_at IS NULL
                """,
                (
                    status,
                    completed_at,
                    response_status_class,
                    error_kind,
                    event.reservation_id,
                    event.endpoint,
                    event.page_number,
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    """
                    SELECT status, completed_at, response_status_class, error_kind
                    FROM google_weather_usage_events
                    WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                    """,
                    (event.reservation_id, event.endpoint, event.page_number),
                ).fetchone()
                if row is None:
                    raise KeyError("unknown Google Weather usage event")
                if not (
                    row["status"] == status
                    and row["completed_at"] is not None
                    and row["response_status_class"] == response_status_class
                    and row["error_kind"] == error_kind
                ):
                    raise GoogleUsageLifecycleError(
                        "Google Weather event must be dispatched before completion"
                    )
        return self.event(event)

    def event(self, event: GoogleUsageEvent) -> GoogleUsageEventState:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT reservation_id, endpoint, page_number, status,
                       billable_events, dispatched_at, completed_at,
                       response_status_class, error_kind
                FROM google_weather_usage_events
                WHERE reservation_id = ? AND endpoint = ? AND page_number = ?
                """,
                (event.reservation_id, event.endpoint, event.page_number),
            ).fetchone()
        if row is None:
            raise KeyError("unknown Google Weather usage event")
        return GoogleUsageEventState(**dict(row))

    def usage(self, *, now: datetime | None = None) -> GoogleUsageCounts:
        instant = _aware_utc(now)
        pacific = instant.astimezone(PACIFIC)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(
                        CASE WHEN billing_date_pacific = ?
                             THEN billable_events ELSE 0 END
                    ), 0) AS daily_events,
                    COALESCE(SUM(billable_events), 0) AS monthly_events
                FROM google_weather_usage_events
                WHERE billing_month = ?
                  AND status IN ('reserved', 'consumed', 'success')
                """,
                (pacific.date().isoformat(), pacific.strftime("%Y-%m")),
            ).fetchone()
            return GoogleUsageCounts(
                daily_events=int(row["daily_events"]),
                monthly_events=int(row["monthly_events"]),
            )
