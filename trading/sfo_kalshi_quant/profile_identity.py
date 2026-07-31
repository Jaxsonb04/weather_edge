"""Canonical published identities for live and isolated research evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .account import LIVE_STABILITY_ACCOUNT_ID, SHARED_ACCOUNT_ID
from .research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V1,
    TARGET_POLICY_V2,
    TARGET_POLICY_V3,
    TARGET_POLICY_V4,
    TARGET_POLICY_V5,
)


def published_profile_key(
    risk_profile: object,
    *,
    research_sleeve: object = None,
    account_id: object = None,
    research_policy_version: object = None,
    policy_fingerprint: object = None,
) -> str:
    """Return the one profile key used by every public report.

    Execution still uses the ``research`` strategy configuration for both
    isolated sleeves.  Publication must keep their evidence separate.
    """

    raw = str(risk_profile or "unknown").strip().lower() or "unknown"
    sleeve = str(research_sleeve or "").strip().lower()
    account = str(account_id or "").strip()
    version = str(research_policy_version or "").strip()
    fingerprint = str(policy_fingerprint or "").strip()
    for policy, published in (
        (TARGET_POLICY_V1, "research-target-v1"),
        (TARGET_POLICY_V2, "research-target-v2"),
        (TARGET_POLICY_V3, "research-target-v3"),
        (TARGET_POLICY_V4, "research-target-v4"),
        (TARGET_POLICY_V5, "research-target-v5"),
        (TARGET_POLICY, "research-target"),
        (MOTION_POLICY, "research-motion"),
    ):
        if (
            raw in {"unknown", "research", published}
            and account == policy.account_id
            and sleeve == policy.sleeve.value
            and version == policy.policy_version
            and fingerprint == policy.policy_fingerprint
        ):
            return published

    # Canonical sleeve evidence is all-or-nothing. A partial, crossed, stale,
    # or forged tuple must not inherit a public profile from any one marker.
    if (
        raw
        in {
            "research-target-v1",
            "research-target-v2",
            "research-target-v3",
            "research-target-v4",
            "research-target-v5",
            "research-target",
            "research-motion",
        }
        or account
        in {
            TARGET_POLICY_V1.account_id,
            TARGET_POLICY_V2.account_id,
            TARGET_POLICY_V3.account_id,
            TARGET_POLICY_V4.account_id,
            TARGET_POLICY_V5.account_id,
            TARGET_POLICY.account_id,
            MOTION_POLICY.account_id,
        }
        or bool(sleeve or version or fingerprint)
    ):
        return "unknown"

    # The fresh live ledger is intentionally a distinct performance era.
    # Legacy shared/null live rows remain readable without blending into it.
    if raw in {"unknown", "live", "live-legacy"}:
        if account == LIVE_STABILITY_ACCOUNT_ID:
            return "live"
        if not account or account == SHARED_ACCOUNT_ID:
            return "live-legacy"
        return "unknown"
    return raw


def execution_profile_key(profile: object) -> str:
    """Map a published profile back to its strategy-configuration identity."""

    published = str(profile or "unknown").strip().lower() or "unknown"
    if published in {
        "research",
        "research-target-v1",
        "research-target-v2",
        "research-target-v3",
        "research-target-v4",
        "research-target-v5",
        "research-target",
        "research-motion",
    }:
        return "research"
    if published == "live-legacy":
        return "live"
    return published


def row_published_profile_key(row: Any) -> str:
    """Resolve a mapping/SQLite row without coupling report modules together."""

    return published_profile_key(
        _row_value(row, "risk_profile"),
        research_sleeve=_row_value(row, "research_sleeve"),
        account_id=_row_value(row, "account_id"),
        research_policy_version=_row_value(row, "research_policy_version"),
        policy_fingerprint=_row_value(row, "policy_fingerprint"),
    )


def _row_value(row: Any, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        keys = row.keys()
    except (AttributeError, TypeError):
        keys = ()
    if key not in keys:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None
