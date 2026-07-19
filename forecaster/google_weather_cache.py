#!/usr/bin/env python3
"""Compatibility entry point for the SFO Google Weather blend refresh.

The implementation is split by responsibility, while this module preserves the
historical import surface and direct-file systemd invocation. Assignments made
to this module are mirrored into the focused modules so existing operational
and test monkeypatch seams keep working.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from types import ModuleType as _ModuleType

import blend_archive as _blend_archive
import blend_learners as _blend_learners
import blend_sources as _blend_sources
import google_api as _google_api
import weather_cache_config as _config


_IMPLEMENTATION_MODULES = (
    _config,
    _google_api,
    _blend_archive,
    _blend_learners,
    _blend_sources,
)
_EXPORT_OWNERS: dict[str, tuple[_ModuleType, ...]] = {}
_IMPLEMENTATION_ONLY_NAMES = {
    "GoogleFetchError",
    "_fetch_google_json",
    "compute_adaptive_blend_weights",
    "compute_rolling_blend_residual_bias",
    "compute_source_mos_corrections",
    "_compute_source_mos_corrections_from_rows",
    # Task 4 city-aware fetch surface (forecaster/google_api.py): not yet
    # wired into this compatibility facade or its frozen callable inventory
    # (forecaster/tests/google_weather_cache_signatures.json). The multi-city
    # refresh orchestrator (Task 6) imports these from google_api directly.
    "CityConfig",
    "GoogleRuntimeStore",
    "GoogleUsageLedger",
    "HTTPError",
    "URLError",
    "dataclass",
    "date",
    "GoogleCityFetchError",
    "GoogleHourlyRow",
    "GoogleDailyRow",
    "GoogleCurrentRow",
    "GoogleFetchResult",
    "fetch_city_hourly",
    "fetch_city_daily",
    "fetch_city_current",
    "fetch_city_weather",
    "_resolve_instant",
    "_open_google_request",
    "_decode_google_json",
    "_dispatch_google_request",
    "_city_hour_datetime",
    "_parse_hourly_page_rows",
    "_google_civil_display_date",
    "_parse_daily_rows",
    "_parse_current_row",
}

for _module in _IMPLEMENTATION_MODULES:
    for _name in dir(_module):
        if _name.startswith("__"):
            continue
        if _name in _IMPLEMENTATION_ONLY_NAMES:
            continue
        _EXPORT_OWNERS[_name] = _EXPORT_OWNERS.get(_name, ()) + (_module,)
        globals().setdefault(_name, getattr(_module, _name))


class _CompatibilityModule(_ModuleType):
    """Propagate legacy facade assignments to every implementation owner."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for owner in _EXPORT_OWNERS.get(name, ()):
            setattr(owner, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule
del _CompatibilityModule
del _ModuleType


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="fetch a fresh Google forecast")
    parser.add_argument("--force", action="store_true", help="ignore a valid cache")
    args = parser.parse_args()

    target_iso = target_date()
    cache = read_json(CACHE_PATH, {})
    archived_cache = False
    archive_stats = {"daily_rows": 0, "blend_rows": 0, "hourly_rows": 0, "scored": 0}
    if cache.get("available") and cache.get("source") == "Google Weather API forecast.hours":
        archive_stats = archive_forecast(cache)
        archived_cache = True
    else:
        archive_stats = score_archive()

    if cache_matches(cache, target_iso) and not args.force and not args.refresh:
        blends = [
            build_blend_snapshot(cache, blend_target)
            for blend_target in blend_targets(cache, target_iso)
        ]
        cache["blend_snapshots"] = [blend for blend in blends if blend]
        cache["blend_generated_at"] = datetime.now(timezone.utc).isoformat()
        archive_stats = archive_forecast(cache, None, blends)
        write_json(CACHE_PATH, cache)
        print(
            f"reblended cached Google forecast for {target_iso}; "
            f"daily rows {archive_stats['daily_rows']}, "
            f"blend rows {archive_stats['blend_rows']}, "
            f"hourly rows {archive_stats['hourly_rows']}, "
            f"scored {archive_stats['scored']}"
        )
        return

    key = api_key()
    if not key:
        if not cache_matches(cache, target_iso):
            write_json(CACHE_PATH, unavailable("Google Weather cache unavailable."))
        archived = "; archived previous cache" if archived_cache else ""
        print(f"missing {API_KEY_ENV}; Google cache not refreshed{archived}; scored {archive_stats['scored']}")
        return

    usage = load_usage()
    estimated_events = estimated_google_weather_events_per_refresh()
    if not usage_has_budget(usage, estimated_events):
        if not cache_matches(cache, target_iso):
            write_json(CACHE_PATH, unavailable("Google Weather event budget reached."))
        print(
            "Google Weather event budget reached "
            f"(daily {usage.get('daily_events')}/{usage.get('daily_event_budget')}, "
            f"monthly {usage.get('monthly_events')}/{usage.get('monthly_event_budget')}); "
            f"scored {archive_stats['scored']}"
        )
        return

    usage = reserve_google_weather_events(usage, estimated_events)
    write_json(USAGE_PATH, usage)
    maximum_events = max(
        estimated_events,
        max(4, HOURLY_LOOKAHEAD_HOURS // 24 + 2)
        + int(ENABLE_GOOGLE_DAILY_FORECAST)
        + int(ENABLE_GOOGLE_CURRENT_CONDITIONS),
    )
    actual_events = 0
    failure = None
    try:
        raw = fetch_google_forecast(key)
        # A returned payload proves the fetch completed. Keep the conservative
        # reservation unless its reported count validates below.
        actual_events = estimated_events
        reported_events = raw.get("google_weather_events_used")
        if type(reported_events) is int and reported_events > 0:
            actual_events = min(reported_events, maximum_events)
        reconciled_usage = adjust_reserved_google_weather_events(
            usage,
            estimated_events,
            actual_events,
        )
        summary = summarize_forecast(raw, target_iso, reconciled_usage)
    except Exception as exc:
        dispatched_events = getattr(exc, "dispatched_events", None)
        if type(dispatched_events) is int:
            actual_events = min(max(0, dispatched_events), maximum_events)
        failure = exc
    finally:
        usage = adjust_reserved_google_weather_events(
            usage,
            estimated_events,
            actual_events,
        )
        write_json(USAGE_PATH, usage)

    if failure is not None:
        if not cache_matches(cache, target_iso):
            write_json(CACHE_PATH, unavailable("Google Weather request failed."))
        print(
            f"Google Weather request failed without saving a URL: {type(failure).__name__}; "
            f"scored {archive_stats['scored']}"
        )
        return

    # Paid quota is persisted before blending so blend failures cannot discard it.
    write_json(CACHE_PATH, summary)
    write_json(USAGE_PATH, usage)
    blends = [
        build_blend_snapshot(summary, blend_target)
        for blend_target in blend_targets(summary, target_iso)
    ]
    summary["blend_snapshots"] = [blend for blend in blends if blend]
    summary["blend_generated_at"] = datetime.now(timezone.utc).isoformat()
    archive_stats = archive_forecast(summary, raw, blends)
    write_json(CACHE_PATH, summary)
    print(
        f"wrote {CACHE_PATH} and archived to {DB_PATH} for {target_iso}; "
        f"Google Weather events today: {usage['daily_events']}/{usage['daily_event_budget']}; "
        f"month: {usage['monthly_events']}/{usage['monthly_event_budget']}; "
        f"daily rows {archive_stats['daily_rows']}; "
        f"blend rows {archive_stats['blend_rows']}; "
        f"hourly rows {archive_stats['hourly_rows']}; "
        f"scored {archive_stats['scored']}"
    )


if __name__ == "__main__":
    main()
