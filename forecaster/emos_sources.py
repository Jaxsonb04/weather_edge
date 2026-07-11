"""Source-version policy for the rolling-origin EMOS archive."""

from __future__ import annotations

from collections.abc import Iterable


ROLLING_ORIGIN_V1_SOURCE = "rolling_origin"
ROLLING_ORIGIN_V2_SOURCE = "rolling_origin_v2"
ROLLING_ORIGIN_SOURCES = frozenset(
    {ROLLING_ORIGIN_V1_SOURCE, ROLLING_ORIGIN_V2_SOURCE}
)


def preferred_rolling_origin_source(sources: Iterable[str]) -> str | None:
    """Select exactly one archive version, preferring the live-faithful v2."""

    available = set(sources)
    if ROLLING_ORIGIN_V2_SOURCE in available:
        return ROLLING_ORIGIN_V2_SOURCE
    if ROLLING_ORIGIN_V1_SOURCE in available:
        return ROLLING_ORIGIN_V1_SOURCE
    return None
