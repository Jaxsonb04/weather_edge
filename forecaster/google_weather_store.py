"""Google Weather event accounting and short-lived parsed runtime storage.

Only request metadata needed to enforce the event budget belongs in the
permanent weather database. Parsed temperatures live separately in the
TTL-enforced runtime database; raw responses and request secrets live in
neither schema.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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
    """Keep TTL-bound Google content on the production tmpfs."""

    if production and not path.resolve().is_relative_to(
        Path("/run/weatheredge").resolve()
    ):
        raise RuntimeError("Google runtime content must live under /run/weatheredge")


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
    covered_hours INTEGER NOT NULL CHECK(covered_hours BETWEEN 0 AND 24),
    complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
    expires_at TEXT NOT NULL,
    PRIMARY KEY(city_slug, station_id, issued_at, target_date)
);
CREATE INDEX IF NOT EXISTS idx_google_runtime_high_expiry
    ON google_runtime_high(expires_at);
"""


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
        assert_runtime_path(self.db_path, production=production)
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_RUNTIME_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}"
        )
        return connection

    def write_hourly(
        self,
        *,
        city_slug: str,
        station_id: str,
        issued_at: datetime,
        valid_at: datetime,
        temperature_f: float,
        stored_at: datetime | None = None,
    ) -> None:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        issued = _aware_utc(issued_at)
        valid = _aware_utc(valid_at)
        stored = _aware_utc(stored_at)
        temperature = _finite_float("temperature_f", temperature_f)
        expires = stored + GOOGLE_HOURLY_TTL
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO google_hourly_runtime (
                    city_slug, station_id, issued_at, valid_at,
                    temperature_f, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(city_slug, station_id, issued_at, valid_at)
                DO UPDATE SET
                    temperature_f = excluded.temperature_f,
                    expires_at = excluded.expires_at
                """,
                (
                    city_slug,
                    station_id,
                    _utc_text(issued),
                    _utc_text(valid),
                    temperature,
                    _utc_text(expires),
                ),
            )

    def active_hourly(
        self,
        *,
        city_slug: str,
        station_id: str,
        now: datetime | None = None,
    ) -> tuple[GoogleHourlyRuntime, ...]:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, valid_at,
                       temperature_f, expires_at
                FROM google_hourly_runtime
                WHERE city_slug = ? AND station_id = ? AND expires_at > ?
                ORDER BY issued_at DESC, valid_at
                """,
                (city_slug, station_id, _utc_text(now)),
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
    ) -> None:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        city = CITY_BY_SLUG[city_slug]
        issued = _aware_utc(issued_at)
        stored = _aware_utc(stored_at)
        target = _date_text("target_date", target_date)
        high = _finite_float("high_f", high_f)
        local_today = stored.astimezone(ZoneInfo(city.civil_tz_name)).date()
        ttl = (
            GOOGLE_TODAY_DAILY_TTL
            if date.fromisoformat(target) <= local_today
            else GOOGLE_FUTURE_DAILY_TTL
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO google_daily_runtime (
                    city_slug, station_id, issued_at, target_date,
                    high_f, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(city_slug, station_id, issued_at, target_date)
                DO UPDATE SET
                    high_f = excluded.high_f,
                    expires_at = excluded.expires_at
                """,
                (
                    city_slug,
                    station_id,
                    _utc_text(issued),
                    target,
                    high,
                    _utc_text(stored + ttl),
                ),
            )

    def active_daily(
        self,
        *,
        city_slug: str,
        station_id: str,
        now: datetime | None = None,
    ) -> tuple[GoogleDailyRuntime, ...]:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, target_date,
                       high_f, expires_at
                FROM google_daily_runtime
                WHERE city_slug = ? AND station_id = ? AND expires_at > ?
                ORDER BY target_date, issued_at DESC
                """,
                (city_slug, station_id, _utc_text(now)),
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
    ) -> None:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        issued = _aware_utc(issued_at)
        observed = _aware_utc(observed_at)
        stored = _aware_utc(stored_at)
        temperature = _finite_float("temperature_f", temperature_f)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO google_current_runtime (
                    city_slug, station_id, issued_at, observed_at,
                    temperature_f, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(city_slug, station_id, issued_at, observed_at)
                DO UPDATE SET
                    temperature_f = excluded.temperature_f,
                    expires_at = excluded.expires_at
                """,
                (
                    city_slug,
                    station_id,
                    _utc_text(issued),
                    _utc_text(observed),
                    temperature,
                    _utc_text(stored + GOOGLE_CURRENT_TTL),
                ),
            )

    def active_current(
        self,
        *,
        city_slug: str,
        station_id: str,
        now: datetime | None = None,
    ) -> GoogleCurrentRuntime | None:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, observed_at,
                       temperature_f, expires_at
                FROM google_current_runtime
                WHERE city_slug = ? AND station_id = ? AND expires_at > ?
                ORDER BY issued_at DESC, observed_at DESC
                LIMIT 1
                """,
                (city_slug, station_id, _utc_text(now)),
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
        high_f: float,
        covered_hours: int,
        complete: bool,
        constituent_expiries: tuple[datetime, ...],
    ) -> None:
        city_slug, station_id = _canonical_city_station(city_slug, station_id)
        issued = _aware_utc(issued_at)
        target = _date_text("target_date", target_date)
        high = _finite_float("high_f", high_f)
        if type(covered_hours) is not int or not 0 <= covered_hours <= 24:
            raise ValueError("covered_hours must be an integer from 0 to 24")
        if type(complete) is not bool:
            raise ValueError("complete must be a boolean")
        if not constituent_expiries:
            raise ValueError("constituent_expiries must not be empty")
        expiries = tuple(_aware_utc(value) for value in constituent_expiries)
        expires = min(expiries)
        with self._connect() as connection:
            connection.execute(
                """
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
                (
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
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT city_slug, station_id, issued_at, target_date, high_f,
                       covered_hours, complete, expires_at
                FROM google_runtime_high
                WHERE city_slug = ? AND station_id = ? AND target_date = ?
                  AND expires_at > ?
                ORDER BY issued_at DESC
                LIMIT 1
                """,
                (city_slug, station_id, target, _utc_text(now)),
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
        with self._connect() as connection:
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
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
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
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}"
        )
        return connection

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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
