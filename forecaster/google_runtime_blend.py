"""Fixed, deterministic Google Weather research challenger (Task 5).

Derives a station-day high ONLY from a complete fixed-standard 24-hour
station day, and computes a fixed, versioned research challenger against
the permanent EMOS baseline. The challenger constants below are predeclared
rather than fitted.

``GoogleRuntimeStore.write_runtime_high`` validates the *shape* of the
constituents it is given (identity, exact hour boundaries, uniqueness) but
never cross-checks them against the store's own stored hourly rows -- this
module is that integrity layer. ``derive_station_day_high`` reads the
active hourly rows directly from the store, groups them by exact
fixed-standard station-day hour, and fails closed (returns ``None``,
writes nothing) whenever there is no matching hourly data at all or a
duplicate/colliding hour is found. It never interpolates a missing hour.

A result with fewer than 24 covered hours is still persisted -- matching
``GoogleRuntimeStore``'s own tested partial-coverage behavior, so partial
same-day "remaining heat" stays queryable -- but its ``complete`` field is
``False``, and ``challenger_from_runtime_high`` refuses to treat it as a
usable high: missing, incomplete, or corroboration-blocked evidence always
yields no adjusted output, and the baseline is never mutated.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from cities import CityConfig
from google_weather_store import (
    GoogleHourlyRuntime,
    GoogleRuntimeHigh,
    GoogleRuntimeStore,
)


# Predeclared and frozen -- see module docstring. Deliberately NOT sourced
# from weather_cache_config.py's env-overridable legacy GOOGLE_DAILY_*
# weights: those belong to the compatibility-migration SFO blend path this
# module must never touch.
GOOGLE_CHALLENGER_SHARE = 0.15
GOOGLE_CHALLENGER_ADJUSTMENT_CAP_F = 1.5
GOOGLE_CHALLENGER_CORROBORATION_BLOCK_GAP_F = 7.0
GOOGLE_CHALLENGER_POLICY_VERSION = "google-runtime-fixed-v1"
GOOGLE_CHALLENGER_FORECAST_ACTION = "forecast"
GOOGLE_CHALLENGER_BLOCK_ACTION = "external_runtime_corroboration_block"


@dataclass(frozen=True)
class GoogleChallenger:
    mu: float | None
    sigma: float
    action: str
    policy_version: str = GOOGLE_CHALLENGER_POLICY_VERSION


def _capped_google_adjustment(gap_f: float) -> float:
    """Clamp the fixed 15% Google share to +/-1.5F.

    Under the frozen constants above, ``GOOGLE_CHALLENGER_CORROBORATION_BLOCK_GAP_F``
    (7F) always fires before ``GOOGLE_CHALLENGER_SHARE * gap_f`` can reach the
    cap (which needs a 10F gap) -- so this branch never actually saturates
    through ``google_challenger`` today. It is kept, and tested directly, as
    an explicit independent safety bound rather than being inlined and
    unverifiable.
    """

    return max(
        -GOOGLE_CHALLENGER_ADJUSTMENT_CAP_F,
        min(GOOGLE_CHALLENGER_ADJUSTMENT_CAP_F, GOOGLE_CHALLENGER_SHARE * gap_f),
    )


def _finite_float(name: str, value: object) -> float:
    """Reject non-finite/non-numeric input before it reaches the formula.

    Mirrors ``google_weather_store._finite_float``. Without this, a NaN
    ``google_high`` bypasses the 7F corroboration block (``nan >= 7.0`` is
    always False) and ``_capped_google_adjustment`` on a NaN gap fabricates
    a confident +/-1.5F adjustment via ``min``/``max`` against NaN; a NaN
    ``baseline_mu``/``baseline_sigma`` would silently poison the persisted
    challenger the same way. Fail closed instead.
    """

    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def google_challenger(
    baseline_mu: float, baseline_sigma: float, google_high: float
) -> GoogleChallenger:
    """Pure, deterministic fixed research challenger.

    ``baseline_mu``/``baseline_sigma`` are the unmodified permanent EMOS
    baseline; this function never mutates them, has no side effects, and
    returns identical output for identical input every time. When the
    Google-derived high disagrees with the baseline by 7F or more, this
    yields the ``external_runtime_corroboration_block`` action with no mean
    rather than a tradeable probability (spec section 7.3).
    """

    baseline_mu = _finite_float("baseline_mu", baseline_mu)
    baseline_sigma = _finite_float("baseline_sigma", baseline_sigma)
    google_high = _finite_float("google_high", google_high)

    gap = google_high - baseline_mu
    if abs(gap) >= GOOGLE_CHALLENGER_CORROBORATION_BLOCK_GAP_F:
        return GoogleChallenger(
            mu=None,
            sigma=baseline_sigma,
            action=GOOGLE_CHALLENGER_BLOCK_ACTION,
        )
    adjustment = _capped_google_adjustment(gap)
    return GoogleChallenger(
        mu=baseline_mu + adjustment,
        sigma=baseline_sigma,
        action=GOOGLE_CHALLENGER_FORECAST_ACTION,
    )


def challenger_from_runtime_high(
    runtime_high: GoogleRuntimeHigh | None,
    *,
    baseline_mu: float,
    baseline_sigma: float,
) -> GoogleChallenger | None:
    """Compose the fixed challenger from a derived station-day high.

    Fails closed -- returns ``None``, so the permanent EMOS baseline is used
    completely unmodified -- whenever the Google evidence is missing or was
    derived from an incomplete fixed-standard station day.
    """

    if runtime_high is None or not runtime_high.complete:
        return None
    return google_challenger(baseline_mu, baseline_sigma, runtime_high.high_f)


def _group_station_day_hours(
    rows: Sequence[GoogleHourlyRuntime],
    *,
    target: date,
    standard_tz: timezone,
) -> tuple[datetime, tuple[GoogleHourlyRuntime, ...]] | None:
    """Group one issue generation's rows into exact station-day hours.

    Fails closed (returns ``None``) on any identity or coverage anomaly:
    rows claiming more than one issue generation, or two rows colliding on
    the same fixed-standard station-day hour. Rows that are not exactly on
    an hour boundary, or fall outside ``target``, are silently excluded (not
    an anomaly by themselves -- they simply do not contribute an hour),
    which can still result in an incomplete day.
    """

    by_hour: dict[int, GoogleHourlyRuntime] = {}
    issued_at: datetime | None = None
    for row in rows:
        local_valid = row.valid_at.astimezone(standard_tz)
        if local_valid.date() != target:
            continue
        if local_valid.minute or local_valid.second or local_valid.microsecond:
            continue
        if issued_at is None:
            issued_at = row.issued_at
        elif row.issued_at != issued_at:
            return None
        hour = local_valid.hour
        if hour in by_hour:
            return None
        by_hour[hour] = row
    if not by_hour or issued_at is None:
        return None
    return issued_at, tuple(by_hour[hour] for hour in sorted(by_hour))


def derive_station_day_high(
    store: GoogleRuntimeStore,
    *,
    city: CityConfig,
    target_date: str,
    now: datetime | None = None,
) -> GoogleRuntimeHigh | None:
    """Derive and persist one station-day's Google high from stored hourly rows.

    Reads directly from the runtime store -- never fabricates or
    interpolates a reading -- and fails closed (returns ``None``, writes
    nothing) when there is no matching hourly data or a duplicate/colliding
    hour is found. See the module docstring for why a result with fewer
    than 24 covered hours is still persisted but must never be treated as a
    usable high.
    """

    station_id = city.nws_station_id
    active_hours = store.active_hourly(
        city_slug=city.slug, station_id=station_id, now=now
    )
    if not active_hours:
        return None
    target = date.fromisoformat(target_date)
    grouped = _group_station_day_hours(
        active_hours, target=target, standard_tz=city.fixed_standard_timezone()
    )
    if grouped is None:
        return None
    issued_at, constituents = grouped
    try:
        store.write_runtime_high(
            city_slug=city.slug,
            station_id=station_id,
            issued_at=issued_at,
            target_date=target_date,
            constituents=constituents,
        )
    except ValueError:
        return None
    return store.active_runtime_high(
        city_slug=city.slug,
        station_id=station_id,
        target_date=target_date,
        now=now,
    )
