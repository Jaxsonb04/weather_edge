#!/usr/bin/env python3
"""Temporary Apple WeatherKit forecast source for WeatherEdge.

Apple's WeatherKit terms prohibit building a secondary historical database
from Apple Weather Data.  This module therefore keeps only the current,
normalized station-day high in a mode-0600 runtime cache and expires it at the
earliest ``metadata.expireTime`` of the products used to derive it.  It never
writes Apple values to ``weather.db`` or a public artifact.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cities import CityConfig, get_city, parse_city_slugs
from settlement_calendar import utc_window_for_local_standard_date


WEATHERKIT_API_ROOT = "https://weatherkit.apple.com/api/v1/weather"
WEATHERKIT_LANGUAGE = "en-US"
WEATHERKIT_DATASETS = "forecastHourly,forecastDaily"
WEATHERKIT_LOOKAHEAD_HOURS = 72
WEATHERKIT_HTTP_TIMEOUT_SECONDS = 15
WEATHERKIT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
APPLE_WEATHER_RUNTIME_CACHE_FILENAME = "apple_weather_runtime.json"
APPLE_WEATHER_RUNTIME_CACHE_PATH = Path(
    os.getenv(
        "APPLE_WEATHER_RUNTIME_CACHE_PATH",
        f"/run/weatheredge/{APPLE_WEATHER_RUNTIME_CACHE_FILENAME}",
    )
)


class WeatherKitError(RuntimeError):
    """Sanitized WeatherKit failure that never retains a token or URL."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep the WeatherKit bearer token on Apple's exact configured host."""

    def redirect_request(
        self,
        request,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        return None


_WEATHERKIT_OPENER = build_opener(_NoRedirectHandler())


def _weatherkit_urlopen(request: Request, *, timeout: int):
    return _WEATHERKIT_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True)
class WeatherKitCredentials:
    team_id: str
    service_id: str
    key_id: str
    private_key_path: Path


class WeatherKitTokenProvider:
    """Generate and memory-cache Apple's required ES256 developer token."""

    def __init__(
        self,
        credentials: WeatherKitCredentials,
        *,
        now_provider: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self._credentials = credentials
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl
        self._cached_token: str | None = None
        self._cached_until: datetime | None = None

    def token(self) -> str:
        now = self._now_provider().astimezone(timezone.utc)
        if (
            self._cached_token is not None
            and self._cached_until is not None
            and now < self._cached_until - timedelta(seconds=60)
        ):
            return self._cached_token

        path = Path(self._credentials.private_key_path)
        try:
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("unsafe key type")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError("unsafe key permissions")
            private_key = path.read_bytes()
            import jwt

            expires_at = now + self._ttl
            token = jwt.encode(
                {
                    "iss": self._credentials.team_id,
                    "iat": int(now.timestamp()),
                    "exp": int(expires_at.timestamp()),
                    "sub": self._credentials.service_id,
                },
                private_key,
                algorithm="ES256",
                headers={
                    "alg": "ES256",
                    "kid": self._credentials.key_id,
                    "id": (
                        f"{self._credentials.team_id}."
                        f"{self._credentials.service_id}"
                    ),
                    "typ": None,
                },
            )
        except Exception:
            raise WeatherKitError("WeatherKit token generation failed") from None
        self._cached_token = token
        self._cached_until = expires_at
        return token


@dataclass(frozen=True)
class AppleHighSnapshot:
    city_slug: str
    station_id: str
    target_date: str
    fetched_at: datetime
    issued_at: datetime
    expires_at: datetime
    hourly_high_f: float
    daily_high_f: float | None
    covered_hours: int
    complete: bool


@dataclass(frozen=True)
class AppleRefreshReport:
    refreshed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class AppleCompatibilityReport:
    """Operational counts only; no provider values or derived comparisons."""

    requested_cities: int
    apple_targets: int
    paired_targets: int
    unpaired_targets: int
    unavailable_cities: int


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise WeatherKitError(f"WeatherKit response has invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise WeatherKitError(f"WeatherKit response has invalid {field}") from None
    if parsed.tzinfo is None:
        raise WeatherKitError(f"WeatherKit response has invalid {field}")
    return parsed.astimezone(timezone.utc)


def _finite_number(value: object, *, field: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise WeatherKitError(f"WeatherKit response has invalid {field}")
    return float(value)


def _c_to_f(value: object, *, field: str) -> float:
    return _finite_number(value, field=field) * 9.0 / 5.0 + 32.0


def _product(
    payload: dict[str, Any],
    name: str,
    *,
    city: CityConfig,
) -> tuple[dict[str, Any], datetime, datetime]:
    product = payload.get(name)
    if not isinstance(product, dict):
        raise WeatherKitError(f"WeatherKit response is missing {name}")
    metadata = product.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("temporarilyUnavailable") is True:
        raise WeatherKitError(f"WeatherKit response has unavailable {name}")
    if metadata.get("units") != "m":
        raise WeatherKitError(f"WeatherKit response has invalid {name} units")
    latitude = _finite_number(metadata.get("latitude"), field=f"{name}.latitude")
    longitude = _finite_number(metadata.get("longitude"), field=f"{name}.longitude")
    if abs(latitude - city.latitude) > 0.05 or abs(longitude - city.longitude) > 0.05:
        raise WeatherKitError(f"WeatherKit response has mismatched {name} location")
    version = metadata.get("version")
    if type(version) is not int or version < 1:
        raise WeatherKitError(f"WeatherKit response has invalid {name} version")
    read_at = _parse_datetime(metadata.get("readTime"), field=f"{name}.readTime")
    expires_at = _parse_datetime(metadata.get("expireTime"), field=f"{name}.expireTime")
    return product, read_at, expires_at


def _daily_diagnostic_highs(
    product: dict[str, Any], *, fixed_tz
) -> dict[date, float]:
    days = product.get("days")
    if not isinstance(days, list):
        raise WeatherKitError("WeatherKit response has invalid forecastDaily.days")
    daily_highs: dict[date, float] = {}
    for raw_day in days:
        if not isinstance(raw_day, dict):
            raise WeatherKitError("WeatherKit response has invalid daily row")
        start_at = _parse_datetime(
            raw_day.get("forecastStart"), field="daily.forecastStart"
        )
        end_at = _parse_datetime(
            raw_day.get("forecastEnd"), field="daily.forecastEnd"
        )
        target = start_at.astimezone(fixed_tz).date()
        expected_start, expected_end = utc_window_for_local_standard_date(
            target, fixed_tz
        )
        if start_at != expected_start or end_at != expected_end:
            continue
        daily_highs[target] = _c_to_f(
            raw_day.get("temperatureMax"), field="temperatureMax"
        )
    return daily_highs


def parse_station_highs(
    payload: dict[str, Any],
    *,
    city: CityConfig,
    fetched_at: datetime,
) -> tuple[AppleHighSnapshot, ...]:
    """Derive fixed-standard station-day highs from one WeatherKit response."""

    if not isinstance(payload, dict):
        raise WeatherKitError("WeatherKit response must be an object")
    hourly, hourly_read_at, hourly_expires_at = _product(
        payload, "forecastHourly", city=city
    )
    if hourly_expires_at <= fetched_at:
        raise WeatherKitError("WeatherKit hourly response is already expired")
    fixed_tz = city.fixed_standard_timezone()
    raw_daily = payload.get("forecastDaily")
    daily_metadata = raw_daily.get("metadata") if isinstance(raw_daily, dict) else None
    daily_available = (
        isinstance(raw_daily, dict)
        and isinstance(daily_metadata, dict)
        and daily_metadata.get("temporarilyUnavailable") is not True
    )
    daily_highs: dict[date, float] = {}
    expires_at = hourly_expires_at
    if daily_available:
        try:
            daily, _daily_read_at, daily_expires_at = _product(
                payload, "forecastDaily", city=city
            )
            if daily_expires_at > fetched_at:
                daily_highs = _daily_diagnostic_highs(
                    daily, fixed_tz=fixed_tz
                )
                if daily_highs:
                    expires_at = min(hourly_expires_at, daily_expires_at)
        except WeatherKitError:
            # Daily data is a non-settlement diagnostic. A malformed or
            # independently unavailable daily product cannot hide a valid
            # hourly-derived station high.
            daily_highs = {}
            expires_at = hourly_expires_at
    hours = hourly.get("hours")
    if not isinstance(hours, list):
        raise WeatherKitError("WeatherKit response has invalid forecastHourly.hours")
    by_day: dict[date, dict[int, float]] = {}
    for raw_hour in hours:
        if not isinstance(raw_hour, dict):
            raise WeatherKitError("WeatherKit response has invalid hourly row")
        valid_at = _parse_datetime(raw_hour.get("forecastStart"), field="forecastStart")
        local_valid = valid_at.astimezone(fixed_tz)
        if local_valid.minute or local_valid.second or local_valid.microsecond:
            continue
        temperature_f = _c_to_f(raw_hour.get("temperature"), field="temperature")
        day_hours = by_day.setdefault(local_valid.date(), {})
        if local_valid.hour in day_hours:
            raise WeatherKitError(
                "WeatherKit response has duplicate fixed-standard hour"
            )
        day_hours[local_valid.hour] = temperature_f

    snapshots = []
    for target, day_hours in sorted(by_day.items()):
        covered_hours = len(day_hours)
        snapshots.append(
            AppleHighSnapshot(
                city_slug=city.slug,
                station_id=city.nws_station_id,
                target_date=target.isoformat(),
                fetched_at=fetched_at,
                issued_at=hourly_read_at,
                expires_at=expires_at,
                hourly_high_f=max(day_hours.values()),
                daily_high_f=daily_highs.get(target),
                covered_hours=covered_hours,
                complete=covered_hours == 24 and set(day_hours) == set(range(24)),
            )
        )
    if not snapshots:
        raise WeatherKitError("WeatherKit response has no station-day hours")
    return tuple(snapshots)


class WeatherKitClient:
    """Small authenticated WeatherKit REST client with injected boundaries."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        transport=_weatherkit_urlopen,
        timeout: int = WEATHERKIT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport
        self._timeout = timeout

    def fetch(
        self,
        city: CityConfig,
        *,
        now: datetime,
        now_provider: Callable[[], datetime] | None = None,
    ) -> tuple[AppleHighSnapshot, ...]:
        now = now.astimezone(timezone.utc)
        hourly_start = now.replace(minute=0, second=0, microsecond=0)
        hourly_end = hourly_start + timedelta(hours=WEATHERKIT_LOOKAHEAD_HOURS)
        local_today = now.astimezone(city.fixed_standard_timezone()).date()
        daily_start = datetime.combine(
            local_today,
            datetime.min.time(),
            tzinfo=city.fixed_standard_timezone(),
        )
        daily_end = daily_start + timedelta(days=4)
        query = urlencode(
            {
                "countryCode": "US",
                "dataSets": WEATHERKIT_DATASETS,
                "timezone": city.settlement_tz_name,
                "hourlyStart": hourly_start.isoformat().replace("+00:00", "Z"),
                "hourlyEnd": hourly_end.isoformat().replace("+00:00", "Z"),
                "dailyStart": daily_start.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "dailyEnd": daily_end.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        url = (
            f"{WEATHERKIT_API_ROOT}/{WEATHERKIT_LANGUAGE}/"
            f"{city.latitude:.4f}/{city.longitude:.4f}?{query}"
        )
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise ValueError("empty token")
            request = Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "WeatherEdge/1.0",
                },
                method="GET",
            )
            with self._transport(request, timeout=self._timeout) as response:
                body = response.read(WEATHERKIT_MAX_RESPONSE_BYTES + 1)
            if len(body) > WEATHERKIT_MAX_RESPONSE_BYTES:
                raise ValueError("oversized response")
            payload = json.loads(body)
            received_at = (
                now
                if now_provider is None
                else now_provider().astimezone(timezone.utc)
            )
            return parse_station_highs(
                payload, city=city, fetched_at=received_at
            )
        except WeatherKitError:
            raise
        except Exception:
            # Never retain the request, JWT, private-key path, or upstream body
            # in an exception chain or message.
            raise WeatherKitError(
                f"WeatherKit request failed for {city.slug}"
            ) from None


class AppleRuntimeCache:
    """Atomic, expiring WeatherKit cache; intended for private tmpfs only."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def _exclusive_lock(self):
        """Serialize every cache read-modify-write across services/processes."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError:
            raise WeatherKitError("Apple runtime cache lock is unsafe") from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise WeatherKitError("Apple runtime cache lock is unsafe")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise WeatherKitError(
                    "Apple runtime cache lock has unsafe permissions"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._remove_orphaned_temp_files_unlocked()
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _remove_orphaned_temp_files_unlocked(self) -> None:
        prefix = f".{self.path.name}.tmp."
        for candidate in self.path.parent.iterdir():
            if not candidate.name.startswith(prefix):
                continue
            metadata = os.lstat(candidate)
            if not (
                stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            ):
                raise WeatherKitError("Apple runtime cache temp path is unsafe")
            candidate.unlink()

    @staticmethod
    def _snapshot_from_row(row: object) -> AppleHighSnapshot:
        required_fields = {
            "city_slug",
            "station_id",
            "target_date",
            "fetched_at",
            "issued_at",
            "expires_at",
            "hourly_high_f",
            "daily_high_f",
            "covered_hours",
            "complete",
        }
        if not isinstance(row, dict) or set(row) != required_fields:
            raise WeatherKitError("Apple runtime cache row is invalid")
        city_slug = row["city_slug"]
        if not isinstance(city_slug, str):
            raise WeatherKitError("Apple runtime cache row is invalid")
        try:
            city = get_city(city_slug)
            target = date.fromisoformat(row["target_date"])
        except (KeyError, TypeError, ValueError):
            raise WeatherKitError("Apple runtime cache row is invalid") from None
        if city.slug != city_slug or row["station_id"] != city.nws_station_id:
            raise WeatherKitError("Apple runtime cache row is invalid")
        if target.isoformat() != row["target_date"]:
            raise WeatherKitError("Apple runtime cache row is invalid")
        if type(row["covered_hours"]) is not int or row["covered_hours"] != 24:
            raise WeatherKitError("Apple runtime cache row is invalid")
        if row["complete"] is not True:
            raise WeatherKitError("Apple runtime cache row is invalid")
        fetched_at = _parse_datetime(row["fetched_at"], field="fetched_at")
        expires_at = _parse_datetime(row["expires_at"], field="expires_at")
        if expires_at <= fetched_at:
            raise WeatherKitError("Apple runtime cache row is invalid")
        return AppleHighSnapshot(
            city_slug=city_slug,
            station_id=row["station_id"],
            target_date=row["target_date"],
            fetched_at=fetched_at,
            issued_at=_parse_datetime(row["issued_at"], field="issued_at"),
            expires_at=expires_at,
            hourly_high_f=_finite_number(
                row["hourly_high_f"], field="hourly_high_f"
            ),
            daily_high_f=(
                None
                if row["daily_high_f"] is None
                else _finite_number(row["daily_high_f"], field="daily_high_f")
            ),
            covered_hours=row["covered_hours"],
            complete=True,
        )

    def _read(self) -> list[AppleHighSnapshot]:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise WeatherKitError("Apple runtime cache path is unsafe")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise WeatherKitError("Apple runtime cache has unsafe permissions")
        try:
            payload = json.loads(self.path.read_text())
            if not isinstance(payload, dict) or set(payload) != {
                "version",
                "entries",
            }:
                raise TypeError("cache payload must be an object")
            if payload.get("version") != 1 or type(payload.get("version")) is not int:
                raise TypeError("cache version is invalid")
            rows = payload.get("entries", [])
            if not isinstance(rows, list):
                raise TypeError("cache entries must be a list")
            snapshots = [self._snapshot_from_row(row) for row in rows]
            identities = {
                (row.city_slug, row.target_date) for row in snapshots
            }
            if len(identities) != len(snapshots):
                raise TypeError("cache entries contain duplicate identities")
            return snapshots
        except (
            AttributeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise WeatherKitError("Apple runtime cache is invalid") from None

    def _write(self, rows: Sequence[AppleHighSnapshot]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = []
        for row in rows:
            item = asdict(row)
            for key in ("fetched_at", "issued_at", "expires_at"):
                item[key] = item[key].isoformat()
            serialized.append(item)
        payload = json.dumps({"version": 1, "entries": serialized}, indent=2) + "\n"
        descriptor, temp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.tmp.",
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def replace_city(
        self,
        city_slug: str,
        snapshots: Sequence[AppleHighSnapshot],
        *,
        now: datetime,
    ) -> None:
        with self._exclusive_lock():
            current = [
                row
                for row in self._read()
                if row.city_slug != city_slug and row.expires_at > now
            ]
            current.extend(
                row for row in snapshots if row.complete and row.expires_at > now
            )
            self._write(current)

    def active_highs(
        self, *, city_slug: str, now: datetime
    ) -> tuple[AppleHighSnapshot, ...]:
        with self._exclusive_lock():
            # A read is also a retention boundary: never leave a value that the
            # caller just established is at or past the provider's expiry.
            self._purge_expired_unlocked(now=now)
            return tuple(
                row
                for row in self._read()
                if row.city_slug == city_slug
                and row.complete
                and row.expires_at > now
            )

    def _purge_expired_unlocked(self, *, now: datetime) -> int:
        rows = self._read()
        active = [row for row in rows if row.expires_at > now]
        removed = len(rows) - len(active)
        if not removed:
            return 0
        if active:
            self._write(active)
        else:
            self.path.unlink(missing_ok=True)
        return removed

    def purge_expired(self, *, now: datetime) -> int:
        """Delete every entry at its provider expiry, removing an empty cache."""

        with self._exclusive_lock():
            return self._purge_expired_unlocked(now=now)

    def purge_or_discard_unverifiable(self, *, now: datetime) -> tuple[int, bool]:
        """Enforce retention atomically, discarding data with unknown expiry."""

        with self._exclusive_lock():
            try:
                return self._purge_expired_unlocked(now=now), False
            except WeatherKitError:
                self._discard_unverifiable_unlocked()
                return 0, True

    def _discard_unverifiable_unlocked(self) -> None:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            return
        if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            self.path.unlink()
            return
        raise WeatherKitError("Apple runtime cache path is unsafe")

    def discard_unverifiable(self) -> None:
        """Remove an unreadable cache without following a symlink target."""

        with self._exclusive_lock():
            self._discard_unverifiable_unlocked()


def refresh_apple_weather(
    cities: Sequence[CityConfig],
    *,
    client: WeatherKitClient,
    cache: AppleRuntimeCache,
    now: datetime | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> AppleRefreshReport:
    """Refresh each city independently; one failure never blocks another."""

    if now_provider is None:
        if now is None:
            now_provider = lambda: datetime.now(timezone.utc)
        else:
            frozen_now = now.astimezone(timezone.utc)
            now_provider = lambda: frozen_now
    now = now_provider().astimezone(timezone.utc)
    # If contents cannot be parsed, their provider expiry cannot be proven.
    # Delete the private cache atomically before attempting a fresh request.
    cache.purge_or_discard_unverifiable(now=now)
    refreshed: list[str] = []
    failed: list[str] = []
    for city in cities:
        try:
            dispatched_at = now_provider().astimezone(timezone.utc)
            snapshots = client.fetch(
                city,
                now=dispatched_at,
                now_provider=now_provider,
            )
            accepted_at = now_provider().astimezone(timezone.utc)
            complete = tuple(
                row
                for row in snapshots
                if row.complete and row.expires_at > accepted_at
            )
            if not complete:
                raise WeatherKitError(
                    "WeatherKit response has no complete settlement day"
                )
            cache.replace_city(city.slug, complete, now=accepted_at)
            refreshed.append(city.slug)
        except Exception:
            failed.append(city.slug)
    return AppleRefreshReport(refreshed=tuple(refreshed), failed=tuple(failed))


def probe_baseline_compatibility(
    cities: Sequence[CityConfig],
    *,
    cache: AppleRuntimeCache,
    baseline_db: Path,
    now_provider: Callable[[], datetime] | None = None,
    max_baseline_age: timedelta = timedelta(hours=6),
) -> AppleCompatibilityReport:
    """Pair current Apple targets with valid live EMOS rows, without scoring.

    Read the baseline without schema migrations or database creation. Values
    remain local to this call: only availability/compatibility counts escape.
    A missing or malformed baseline never makes provider refresh fail.
    """

    now_provider = now_provider or (lambda: datetime.now(timezone.utc))
    connection = None
    try:
        connection = sqlite3.connect(
            Path(baseline_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=2,
        )
    except (OSError, sqlite3.Error):
        pass
    targets = paired = unavailable = 0
    try:
        for city in cities:
            read_at = now_provider().astimezone(timezone.utc)
            try:
                snapshots = cache.active_highs(city_slug=city.slug, now=read_at)
            except (OSError, WeatherKitError):
                snapshots = ()
            city_targets = 0
            for snapshot in snapshots:
                try:
                    target = date.fromisoformat(snapshot.target_date)
                    lead = (
                        target - read_at.astimezone(city.fixed_standard_timezone()).date()
                    ).days
                    if (
                        lead not in (1, 2)
                        or snapshot.city_slug != city.slug
                        or snapshot.station_id != city.nws_station_id
                        or not snapshot.complete or snapshot.covered_hours != 24
                    ):
                        continue
                    targets += 1
                    city_targets += 1
                    if connection is None:
                        continue
                    rows = connection.execute(
                        "SELECT predicted_high_f, sigma_f, fetched_at "
                        "FROM forecast_emos_daily_high "
                        "WHERE station_id=? AND target_date=? AND lead_days=? AND source='live'",
                        (city.nws_station_id, snapshot.target_date, lead),
                    ).fetchall()
                    if not rows:
                        continue
                    # Compare parsed instants, not differently formatted ISO
                    # strings. Never fall back past a malformed latest row.
                    stamped = [
                        (_parse_datetime(row[2], field="baseline timestamp"), row)
                        for row in rows
                    ]
                    baseline_at, baseline = max(stamped, key=lambda item: item[0])
                    _finite_number(baseline[0], field="baseline mean")
                    sigma = _finite_number(baseline[1], field="baseline sigma")
                    checked_at = now_provider().astimezone(timezone.utc)
                    if snapshot.expires_at <= checked_at:
                        # This read discovered expiry, so enforce retention now.
                        cache.purge_or_discard_unverifiable(now=checked_at)
                        continue
                    current_lead = (
                        target - checked_at.astimezone(city.fixed_standard_timezone()).date()
                    ).days
                    if (
                        sigma <= 0 or current_lead != lead
                        or not timedelta(0) <= checked_at - baseline_at <= max_baseline_age
                        or snapshot.fetched_at > checked_at or snapshot.issued_at > checked_at
                    ):
                        continue
                    paired += 1
                except (OSError, TypeError, ValueError, OverflowError, sqlite3.Error, WeatherKitError):
                    continue
            if not city_targets:
                unavailable += 1
    finally:
        if connection is not None:
            connection.close()
    return AppleCompatibilityReport(
        requested_cities=len(cities), apple_targets=targets, paired_targets=paired,
        unpaired_targets=targets - paired, unavailable_cities=unavailable,
    )


def _print_baseline_compatibility(
    cities: Sequence[CityConfig], cache: AppleRuntimeCache, baseline_db: Path,
    now_provider: Callable[[], datetime],
) -> None:
    try:
        max_age_hours = float(os.getenv("SFO_FORECAST_MAX_AGE_HOURS", "6"))
        if not math.isfinite(max_age_hours) or max_age_hours <= 0:
            raise ValueError
        report = probe_baseline_compatibility(
            cities, cache=cache, baseline_db=baseline_db, now_provider=now_provider,
            max_baseline_age=timedelta(hours=max_age_hours),
        )
    except (OSError, ValueError, OverflowError, WeatherKitError):
        print("Apple runtime compatibility unavailable; not an accuracy evaluation")
        return
    print(
        "Apple runtime compatibility: "
        f"cities={report.requested_cities}, targets={report.apple_targets}, "
        f"paired={report.paired_targets}, unpaired={report.unpaired_targets}, "
        f"unavailable_cities={report.unavailable_cities}; not an accuracy evaluation"
    )


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _runtime_cache_from_env(
    *, runtime_root: Path = Path("/run/weatheredge")
) -> AppleRuntimeCache:
    path = Path(
        os.getenv(
            "APPLE_WEATHER_RUNTIME_CACHE_PATH",
            str(APPLE_WEATHER_RUNTIME_CACHE_PATH),
        )
    )
    resolved_runtime_root = runtime_root.resolve()
    if (
        path.name != APPLE_WEATHER_RUNTIME_CACHE_FILENAME
        or path.parent.resolve() != resolved_runtime_root
    ):
        raise WeatherKitError(
            "Apple runtime cache must use its canonical transient path"
        )
    return AppleRuntimeCache(path)


def _credentials_from_env() -> WeatherKitCredentials | None:
    values = {
        "team_id": os.getenv("APPLE_WEATHER_TEAM_ID", "").strip(),
        "service_id": os.getenv("APPLE_WEATHER_SERVICE_ID", "").strip(),
        "key_id": os.getenv("APPLE_WEATHER_KEY_ID", "").strip(),
        "private_key_path": os.getenv("APPLE_WEATHER_PRIVATE_KEY_PATH", "").strip(),
    }
    if not all(values.values()):
        return None
    return WeatherKitCredentials(
        team_id=values["team_id"],
        service_id=values["service_id"],
        key_id=values["key_id"],
        private_key_path=Path(values["private_key_path"]),
    )


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    transport=_weatherkit_urlopen,
    now_provider: Callable[[], datetime] | None = None,
    runtime_root: Path | None = None,
) -> int:
    """Run one isolated all-city refresh or an expiry purge."""

    parser = argparse.ArgumentParser(description="Refresh temporary Apple Weather data")
    parser.add_argument(
        "--cities", default="all", help="'all' or comma-separated city slugs"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--purge-only", action="store_true")
    mode.add_argument(
        "--probe-only", action="store_true",
        help="pair cached current targets with live EMOS; no API calls",
    )
    parser.add_argument(
        "--baseline-db", type=Path,
        default=Path(__file__).resolve().parent / "weather.db",
    )
    args = parser.parse_args(argv)
    now_provider = now_provider or (lambda: datetime.now(timezone.utc))
    now = now_provider().astimezone(timezone.utc)
    try:
        cache = _runtime_cache_from_env(
            runtime_root=runtime_root or Path("/run/weatheredge")
        )
    except WeatherKitError:
        print("Apple Weather runtime configuration is invalid; no request dispatched")
        return 2

    if args.purge_only:
        try:
            purged, discarded = cache.purge_or_discard_unverifiable(now=now)
        except WeatherKitError:
            print("Apple Weather runtime cache purge failed")
            return 1
        if discarded:
            print("discarded unverifiable Apple Weather runtime cache")
            return 0
        print(f"purged {purged} expired Apple Weather runtime entries")
        return 0

    if args.probe_only:
        try:
            cities = parse_city_slugs(args.cities)
        except (KeyError, ValueError):
            print("Apple Weather refresh configuration is invalid")
            return 2
        _print_baseline_compatibility(cities, cache, args.baseline_db, now_provider)
        return 0

    credentials = _credentials_from_env()
    if not _env_enabled("ENABLE_APPLE_WEATHER"):
        print("Apple Weather refresh disabled; no request dispatched")
        return 0
    if credentials is None:
        print(
            "Apple Weather refresh enabled but credential configuration is incomplete"
        )
        return 2

    ttl_seconds = _env_int(
        "APPLE_WEATHER_TOKEN_TTL_SECONDS", 900, minimum=120, maximum=3600
    )
    timeout = _env_int(
        # Fifteen sequential city calls must remain inside the service's
        # five-minute deadline even when every endpoint times out.
        "APPLE_WEATHER_HTTP_TIMEOUT_SECONDS",
        15,
        minimum=1,
        maximum=15,
    )
    token_provider = WeatherKitTokenProvider(
        credentials,
        now_provider=now_provider,
        ttl=timedelta(seconds=ttl_seconds),
    )
    client = WeatherKitClient(
        token_provider=token_provider.token,
        transport=transport,
        timeout=timeout,
    )
    try:
        cities = parse_city_slugs(args.cities)
    except (KeyError, ValueError):
        print("Apple Weather refresh configuration is invalid")
        return 2
    try:
        report = refresh_apple_weather(
            cities,
            client=client,
            cache=cache,
            now_provider=now_provider,
        )
    except WeatherKitError:
        print("Apple Weather runtime cache is unsafe")
        return 2
    print(
        "Apple Weather temporary refresh: "
        f"{len(report.refreshed)} refreshed, {len(report.failed)} failed; "
        "live trading weight remains 0"
    )
    _print_baseline_compatibility(cities, cache, args.baseline_db, now_provider)
    return 1 if report.failed else 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
