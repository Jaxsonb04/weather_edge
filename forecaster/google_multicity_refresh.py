"""Budget-safe orchestration of the 15-city Google Weather refresh (Task 6).

Sequences one refresh cycle:

1. Build/archive the non-Google baseline FIRST (``_default_archive_baseline``
   shells out to the existing ``emos_forecast.py --serve-rolling --cities
   all`` pipeline -- the same station-agnostic EMOS baseline the systemd
   refresh unit already runs for every city). This always runs before any
   Google fetch is attempted, and its failure is recorded, never raised, so
   a Google outage can never block/contaminate the baseline and a baseline
   outage can never block Google (see ``_archive_baseline_first``).
2. Purge expired runtime rows from the TTL-enforced runtime store
   (``GoogleRuntimeStore.purge_expired``) -- the "startup purge" every
   producer/consumer of that store performs (spec section 7.2).
3. Refresh every requested city, SFO always first (spec section 7.4: "SFO
   capacity is reserved first"), then the rest ordered by
   ``_priority_order`` -- active exposure, soonest close, oldest
   corroboration, then the configured market-volume order
   (``cities.CITIES``'s existing ordering) as the deterministic fallback
   when no live signal is supplied.

Budget decisions read the PERMANENT LEDGER's authoritative reserve/complete
counts (``GoogleUsageLedger.usage()``) -- never
``google_api.fetch_city_weather``'s ``dispatched_events`` field, which is a
global monthly delta that concurrent processes can inflate and that can read
0 at a Pacific month rollover. This module also does not call
``fetch_city_weather`` at all: it composes the per-endpoint fetchers
(``fetch_city_hourly``/``fetch_city_daily``/``fetch_city_current``) directly,
which is what gives it independent per-endpoint failure isolation within one
city's bundle.

Every city is isolated: one city's failure (fetch, parse, or budget) is
classified and recorded in that city's own ``CityRefreshStatus`` and never
stops -- or is masked by -- any other city's refresh (spec section 10, and
this project's binding per-city isolation contract). Nothing here persists a
raw Google value anywhere but the TTL-enforced runtime store: the
compatibility status this module writes to the legacy JSON cache
(``build_compatibility_status``) contains only availability, per-endpoint
outcome, error kind, and budget counts -- never a temperature, a high, or
response content (plan Task 6 Step 4).

This module is research-fetch orchestration ONLY: it never derives, computes,
or persists a Google-conditioned forecast challenger (that is
``google_runtime_blend``, wired in a later task under its own research-shadow
policy) and never touches the live SFO forecast, EMOS/LSTM training,
adaptive weights, MOS, or residual de-bias paths.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from cities import CITIES, DEFAULT_CITY_SLUG, CityConfig, parse_city_slugs
from google_api import (
    GoogleCityFetchError,
    api_key,
    fetch_city_current,
    fetch_city_daily,
    fetch_city_hourly,
    write_json,
)
from google_weather_store import (
    GoogleRuntimeStore,
    GoogleUsageCounts,
    GoogleUsageLedger,
    GoogleWeatherBudgetExceeded,
)
from weather_cache_config import (
    CACHE_PATH,
    DB_PATH,
    GOOGLE_HOURLY_MAX_PAGES,
    GOOGLE_RUNTIME_DB_PATH,
    GOOGLE_RUNTIME_PRODUCTION,
)


# SFO: 3 hourly pages + daily + current. Non-SFO: 3 hourly pages + daily
# once daily (no current-conditions endpoint) -- spec section 7.4.
SFO_BUNDLE_MAX_EVENTS = GOOGLE_HOURLY_MAX_PAGES + 2
NON_SFO_BUNDLE_MAX_EVENTS = GOOGLE_HOURLY_MAX_PAGES + 1

# The existing non-Google baseline pipeline the systemd refresh unit already
# runs for every city (trading/deploy/aws/systemd/sfo-forecaster-refresh.service.in).
# Invoked as a subprocess rather than imported so this module never depends
# on (or is blamed for) emos_forecast.py's own live-model network calls.
_BASELINE_SCRIPT = Path(__file__).with_name("emos_forecast.py")
_BASELINE_SUBPROCESS_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class CityPriorityHint:
    """Optional live-trading signal for ordering non-SFO city refreshes.

    Every field defaults to "no signal": a city with no injected hint always
    sorts after any city that has one, before falling back to the fixed
    market-volume order in ``cities.CITIES`` (see ``_priority_sort_key``).
    """

    active_exposure: float = 0.0
    soonest_close_at: datetime | None = None
    corroboration_age_seconds: float = 0.0


@dataclass(frozen=True)
class BaselineResult:
    attempted: bool
    succeeded: bool
    error_kind: str | None


@dataclass(frozen=True)
class CityRefreshStatus:
    city_slug: str
    attempted: bool
    available: bool
    endpoints: Mapping[str, str]
    error_kind: str | None
    skipped_reason: str | None


@dataclass(frozen=True)
class MulticityRefreshReport:
    generated_at: datetime
    baseline: BaselineResult
    purged_rows: int
    cities: tuple[CityRefreshStatus, ...]
    daily_events: int
    monthly_events: int
    daily_event_budget: int
    monthly_event_budget: int
    soft_monthly_ceiling: int


def _default_archive_baseline() -> None:
    """Serve the live EMOS baseline for every city before any Google fetch."""

    subprocess.run(
        [sys.executable, str(_BASELINE_SCRIPT), "--serve-rolling", "--cities", "all"],
        check=True,
        cwd=str(_BASELINE_SCRIPT.parent),
        timeout=_BASELINE_SUBPROCESS_TIMEOUT_SECONDS,
        capture_output=True,
    )


def _archive_baseline_first(
    archive_baseline: Callable[[], None] | None,
) -> BaselineResult:
    """Run the baseline archiver and fail soft: never let it raise outward.

    A baseline failure must never block the independent Google refresh that
    follows, and (by construction -- this call always happens first, before
    any Google fetch is attempted) a Google failure can never reach back and
    block or contaminate a baseline that already ran.
    """

    archiver = archive_baseline if archive_baseline is not None else _default_archive_baseline
    try:
        archiver()
    except Exception as exc:
        return BaselineResult(attempted=True, succeeded=False, error_kind=type(exc).__name__)
    return BaselineResult(attempted=True, succeeded=True, error_kind=None)


_MARKET_VOLUME_RANK: dict[str, int] = {city.slug: index for index, city in enumerate(CITIES)}
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _priority_sort_key(city: CityConfig, hints: Mapping[str, CityPriorityHint]):
    hint = hints.get(city.slug) or CityPriorityHint()
    close_at = hint.soonest_close_at if hint.soonest_close_at is not None else _FAR_FUTURE
    return (
        -hint.active_exposure,
        close_at,
        -hint.corroboration_age_seconds,
        _MARKET_VOLUME_RANK[city.slug],
    )


def _priority_order(
    cities: Sequence[CityConfig],
    *,
    priority_hints: Mapping[str, CityPriorityHint] | None,
) -> tuple[CityConfig, ...]:
    """SFO always first; the rest sorted by ``_priority_sort_key``."""

    hints = priority_hints or {}
    sfo = tuple(city for city in cities if city.slug == DEFAULT_CITY_SLUG)
    rest = tuple(city for city in cities if city.slug != DEFAULT_CITY_SLUG)
    rest_sorted = tuple(sorted(rest, key=lambda city: _priority_sort_key(city, hints)))
    return sfo + rest_sorted


def _budget_skip_reason(
    counts: GoogleUsageCounts,
    max_cost: int,
    *,
    is_sfo: bool,
    usage: GoogleUsageLedger,
) -> str | None:
    """Decide, from the ledger's authoritative counts, whether to skip a city.

    Never uses ``fetch_city_weather``'s ``dispatched_events`` -- reads the
    permanent ledger's reserve/complete counts directly. The hard daily and
    monthly caps apply to every city, including SFO; only the soft ceiling
    exempts SFO ("SFO capacity is reserved first" -- spec section 7.4), which
    is how SFO keeps priority once completed+reserved events cross 7,800
    while non-essential (non-SFO) refreshes stop.
    """

    if counts.daily_events + max_cost > usage.daily_budget:
        return "daily_budget"
    if counts.monthly_events + max_cost > usage.monthly_budget:
        return "monthly_budget"
    if not is_sfo and counts.monthly_events + max_cost > usage.soft_monthly_ceiling:
        return "soft_ceiling"
    return None


def _refresh_one_city(
    city: CityConfig,
    *,
    key: str,
    usage: GoogleUsageLedger,
    runtime: GoogleRuntimeStore,
    transport,
    now: datetime,
    include_current: bool,
) -> CityRefreshStatus:
    """Fetch every endpoint for one city, isolating each endpoint's failure.

    Never lets a fetch/budget exception propagate: every outcome (success,
    classified failure, or budget denial) is recorded on this city's status,
    so one city -- or one endpoint within a city -- can never silently stop
    or mask another (this project's per-city isolation contract).
    """

    results: list[tuple[str, str]] = []
    error_kind: str | None = None

    def _attempt(endpoint_name: str, action: Callable[[], object]) -> None:
        nonlocal error_kind
        try:
            action()
            results.append((endpoint_name, "success"))
        except GoogleWeatherBudgetExceeded:
            results.append((endpoint_name, "budget_exceeded"))
            if error_kind is None:
                error_kind = "budget"
        except GoogleCityFetchError:
            results.append((endpoint_name, "failed"))
            if error_kind is None:
                error_kind = endpoint_name
        except Exception:
            results.append((endpoint_name, "failed"))
            if error_kind is None:
                error_kind = "unexpected"

    _attempt(
        "hourly",
        lambda: fetch_city_hourly(
            city,
            key=key,
            usage=usage,
            runtime=runtime,
            max_pages=GOOGLE_HOURLY_MAX_PAGES,
            transport=transport,
            now=now,
        ),
    )
    _attempt(
        "daily",
        lambda: fetch_city_daily(
            city, key=key, usage=usage, runtime=runtime, transport=transport, now=now
        ),
    )
    if include_current:
        _attempt(
            "current",
            lambda: fetch_city_current(
                city, key=key, usage=usage, runtime=runtime, transport=transport, now=now
            ),
        )

    endpoints = dict(results)
    available = any(status == "success" for _, status in results)
    return CityRefreshStatus(
        city_slug=city.slug,
        attempted=True,
        available=available,
        endpoints=endpoints,
        error_kind=error_kind,
        skipped_reason=None,
    )


def refresh_all_cities(
    *,
    cities: Sequence[CityConfig] = CITIES,
    key: str | None,
    usage: GoogleUsageLedger,
    runtime: GoogleRuntimeStore,
    transport=urlopen,
    now: datetime | None = None,
    archive_baseline: Callable[[], None] | None = None,
    priority_hints: Mapping[str, CityPriorityHint] | None = None,
) -> MulticityRefreshReport:
    """Run one budget-safe Google Weather refresh cycle for ``cities``."""

    fetch_instant = now if now is not None else datetime.now(timezone.utc)

    baseline = _archive_baseline_first(archive_baseline)
    purged_rows = runtime.purge_expired(now=fetch_instant)

    statuses: list[CityRefreshStatus] = []
    for city in _priority_order(cities, priority_hints=priority_hints):
        is_sfo = city.slug == DEFAULT_CITY_SLUG

        if not key:
            statuses.append(
                CityRefreshStatus(
                    city_slug=city.slug,
                    attempted=False,
                    available=False,
                    endpoints={},
                    error_kind=None,
                    skipped_reason="missing_api_key",
                )
            )
            continue

        max_cost = SFO_BUNDLE_MAX_EVENTS if is_sfo else NON_SFO_BUNDLE_MAX_EVENTS
        counts = usage.usage(now=fetch_instant)
        skip_reason = _budget_skip_reason(counts, max_cost, is_sfo=is_sfo, usage=usage)
        if skip_reason is not None:
            statuses.append(
                CityRefreshStatus(
                    city_slug=city.slug,
                    attempted=False,
                    available=False,
                    endpoints={},
                    error_kind=None,
                    skipped_reason=skip_reason,
                )
            )
            continue

        statuses.append(
            _refresh_one_city(
                city,
                key=key,
                usage=usage,
                runtime=runtime,
                transport=transport,
                now=fetch_instant,
                include_current=is_sfo,
            )
        )

    final_counts = usage.usage(now=fetch_instant)
    return MulticityRefreshReport(
        generated_at=fetch_instant,
        baseline=baseline,
        purged_rows=purged_rows,
        cities=tuple(statuses),
        daily_events=final_counts.daily_events,
        monthly_events=final_counts.monthly_events,
        daily_event_budget=usage.daily_budget,
        monthly_event_budget=usage.monthly_budget,
        soft_monthly_ceiling=usage.soft_monthly_ceiling,
    )


def build_compatibility_status(report: MulticityRefreshReport) -> dict:
    """The only shape this orchestrator ever writes to the legacy JSON cache.

    Status only -- availability, per-endpoint outcome, sanitized error kind,
    and budget counts. Never a temperature, a high, or any other Google
    response content (plan Task 6 Step 4): this function's inputs
    (``MulticityRefreshReport``/``CityRefreshStatus``) structurally never
    carry a raw Google value in the first place.
    """

    return {
        "source": "google_weather_multicity_status",
        "mode": "multicity",
        "generated_at": report.generated_at.isoformat(),
        "budget": {
            "daily_events": report.daily_events,
            "daily_event_budget": report.daily_event_budget,
            "monthly_events": report.monthly_events,
            "monthly_event_budget": report.monthly_event_budget,
            "soft_monthly_ceiling": report.soft_monthly_ceiling,
        },
        "purged_runtime_rows": report.purged_rows,
        "baseline": {
            "attempted": report.baseline.attempted,
            "succeeded": report.baseline.succeeded,
            "error_kind": report.baseline.error_kind,
        },
        "cities": {
            status.city_slug: {
                "attempted": status.attempted,
                "available": status.available,
                "endpoints": dict(status.endpoints),
                "error_kind": status.error_kind,
                "skipped_reason": status.skipped_reason,
            }
            for status in report.cities
        },
    }


def run_cli(cities_arg: str) -> None:
    """The ``--cities`` CLI entry point wired from ``google_weather_cache.main``."""

    cities = parse_city_slugs(cities_arg)
    key = api_key()
    usage = GoogleUsageLedger(DB_PATH)
    runtime = GoogleRuntimeStore(GOOGLE_RUNTIME_DB_PATH, production=GOOGLE_RUNTIME_PRODUCTION)
    report = refresh_all_cities(cities=cities, key=key, usage=usage, runtime=runtime)
    write_json(CACHE_PATH, build_compatibility_status(report))

    attempted = sum(1 for status in report.cities if status.attempted)
    available = sum(1 for status in report.cities if status.available)
    print(
        f"refreshed Google weather for {len(report.cities)} cities "
        f"({attempted} attempted, {available} available); "
        f"events today: {report.daily_events}/{report.daily_event_budget}; "
        f"month: {report.monthly_events}/{report.monthly_event_budget}; "
        f"runtime rows purged: {report.purged_rows}"
    )
