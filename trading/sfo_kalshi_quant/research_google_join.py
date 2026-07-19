"""Task 7: join derived Google-challenger evidence onto historical rows.

Binding Google-evidence join contract (Task 3 final review, J1-J6):

- **J1**: source Google evidence from the paper-DB ``google_challenger_snapshots``
  table (``trading/sfo_kalshi_quant/store/schema.py``) -- the SAME database
  ``scan_context_snapshots`` lives in, via a single caller-supplied
  ``sqlite3.Connection`` -- never weather.db's baseline table.
- **J2**: join per source-context GROUP (post-dedup), not per raw profile
  row. ``research_walkforward.load_research_cases`` already groups raw,
  per-profile ``scan_context_snapshots`` rows by ``source_context_hash``
  and reconciles them into one canonical ``ResearchCase`` per real-world
  scan (its own ``_reconcile_duplicates``, keyed on the EARLIEST
  ``decision_at`` among the group). This module performs the SAME
  grouping independently, one layer BEFORE that loader ever runs, so it
  can compute one join per group and apply it identically to every row in
  that group -- never a per-raw-row join that could attach different
  Google evidence to two rows the loader is about to collapse into one
  case (which would make ``_reconcile_duplicates`` correctly, but
  needlessly, reject the case as ``inconsistent_duplicate``). The join
  bound is ``MIN(decision_at)`` across the WHOLE group, matching exactly
  what the loader will later treat as that case's own ``decision_at``.
- **J3**: matched evidence is attached as the four raw row keys
  ``research_walkforward._build_google_evidence`` already knows how to
  parse (``google_challenger_action``/``google_challenger_mu``/
  ``google_challenger_sigma``/``google_challenger_policy_version``) --
  this module never constructs a ``GoogleChallengerEvidence`` object
  itself, reusing the existing, reviewed parsing/validation path instead
  of duplicating it. ``challenger_mu`` may be ``None`` for a blocked
  action -- ``_build_google_evidence`` already treats that as correct,
  not missing. The case's own ``baseline_mu``/``baseline_sigma`` row keys
  are never read from, or overwritten by, the matched snapshot.
- **J4**: no matching snapshot (including every snapshot rejected by the
  J2 point-in-time bound) omits ALL FOUR keys entirely -- never a partial
  set. A partially-attached set of the four keys is corruption by
  ``_build_google_evidence``'s own design (it fails the WHOLE case
  closed, ``invalid_google_evidence``, on a partial set) -- this module
  structurally cannot produce one, since the four keys are only ever
  attached together, from one dict literal, in one place below.
- **J5**: vintage coherence. A matched snapshot's own ``baseline_mu``/
  ``baseline_sigma`` must equal the row group's own ``baseline_mu``/
  ``baseline_sigma`` (the SAME baseline distribution the Google shadow
  challenger was computed against, per
  ``google_challenger_shadow.build_google_challenger_snapshot``) --
  otherwise the "challenger" would be conditioned on a DIFFERENT
  baseline than the one this case actually replays against, silently
  mixing vintages. A mismatch attaches NOTHING (falls through to J4's
  omit-all-four behavior) and is separately recorded as a
  ``GoogleJoinSkip`` with reason ``GOOGLE_JOIN_REASON_VINTAGE_MISMATCH``
  -- this is diagnostic evidence for a research report, never a
  ``CaseSkip`` (the row itself is still usable; it simply carries no
  Google evidence, the same as a row with no matching snapshot at all).
- **J6**: this module IS the piece research_walkforward.py's and
  research_candidates.py's own docstrings, at Task 2/3 authorship time,
  described as "has not landed" / "Google Task 7 will add" -- both are
  updated (this task) to point here instead of describing a future gap.

This module never talks to the wall clock or unseeded random state. It
takes a caller-supplied ``sqlite3.Connection`` (J1) and is otherwise a
pure function of ``rows``/query results: the same set of rows, in any
order, against the same snapshot table content, always produces the same
joined output.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from ._util import _parse_timestamp
from .db import source_neutral_context_from_scan_context_row
from .research_walkforward import GOOGLE_CHALLENGER_POLICY_VERSION

GOOGLE_JOIN_REASON_VINTAGE_MISMATCH = "google_evidence_vintage_mismatch"

# The four raw-row keys this module ever attaches, always together (J3/J4).
GOOGLE_JOIN_ATTACHED_KEYS = (
    "google_challenger_action",
    "google_challenger_mu",
    "google_challenger_sigma",
    "google_challenger_policy_version",
)

# Loose float-equality tolerance for the J5 vintage-coherence check. Both
# values round-trip through the SAME IEEE-754 double representation (Python
# float in, SQLite REAL storage, Python float back out) with no arithmetic
# in between, so an exact match is the ordinary case; this tolerance exists
# only to guard against an unforeseen storage/precision artifact, never to
# paper over a genuinely different baseline.
_VINTAGE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class GoogleJoinSkip:
    """One source-context group whose matched snapshot was rejected, and why."""

    source_context_hash: str
    station_id: str
    target_date: str
    reason: str


@dataclass(frozen=True)
class GoogleJoinResult:
    """``rows`` -- content-identical to the input, except any row whose
    group matched a vintage-coherent snapshot now also carries the four
    ``GOOGLE_JOIN_ATTACHED_KEYS``. ``matched_row_count`` counts individual
    ROWS that received the four keys (a group with several duplicate
    per-profile rows contributes more than one to this count).
    ``skips`` is diagnostic-only (J5) -- it never removes or alters a row."""

    rows: tuple[dict[str, object], ...]
    matched_row_count: int
    skips: tuple[GoogleJoinSkip, ...]


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _floats_close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_VINTAGE_TOLERANCE, abs_tol=_VINTAGE_TOLERANCE)


def _group_rows_by_source_context(
    rows: Sequence[Mapping[str, object]],
) -> tuple[list[str | None], dict[str, dict[str, object]], dict[str, list[int]]]:
    """Independently reproduce (J2) ``load_research_cases``'s own grouping:
    per-row ``source_context_hash`` via the SAME canonicalization helper,
    row indices grouped by that hash, and one representative context per
    group. A row that cannot be canonicalized gets ``None`` in
    ``row_hashes`` (untouched below -- ``load_research_cases`` will skip it
    on its own, independent pass, with its own recorded reason)."""

    row_hashes: list[str | None] = []
    contexts: dict[str, dict[str, object]] = {}
    groups: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        context = source_neutral_context_from_scan_context_row(dict(row))
        if context is None:
            row_hashes.append(None)
            continue
        source_hash = str(context["source_context_hash"])
        row_hashes.append(source_hash)
        contexts.setdefault(source_hash, context)
        groups.setdefault(source_hash, []).append(index)
    return row_hashes, contexts, groups


def _min_decision_at(rows: Sequence[Mapping[str, object]], indices: Sequence[int]) -> datetime | None:
    parsed = [
        moment
        for moment in (_parse_timestamp(rows[i].get("decision_at")) for i in indices)
        if moment is not None
    ]
    return min(parsed) if parsed else None


def _best_snapshot_at_or_before(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    target_date: str,
    policy_version: str,
    at_or_before: datetime,
) -> sqlite3.Row | None:
    """J2: among every stored snapshot for this station/target/policy,
    pick the one with the MAXIMUM ``issued_at`` at or before
    ``at_or_before`` -- the point-in-time guard so nothing downstream can
    ever catch a snapshot issued after the group's own earliest decision.
    Comparison is done in Python (not SQL string ordering) so differing,
    but still valid, ISO8601 timestamp formats can never silently
    misorder."""

    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        "SELECT issued_at, baseline_mu, baseline_sigma, challenger_mu, "
        "challenger_sigma, action FROM google_challenger_snapshots "
        "WHERE station_id = ? AND target_date = ? AND policy_version = ?",
        (station_id, target_date, policy_version),
    ).fetchall()

    best_row: sqlite3.Row | None = None
    best_issued_at: datetime | None = None
    for candidate in candidates:
        issued_at = _parse_timestamp(candidate["issued_at"])
        if issued_at is None or issued_at > at_or_before:
            continue
        if best_issued_at is None or issued_at > best_issued_at:
            best_issued_at = issued_at
            best_row = candidate
    return best_row


def attach_google_challenger_evidence(
    rows: Sequence[Mapping[str, object]],
    conn: sqlite3.Connection,
    *,
    policy_version: str = GOOGLE_CHALLENGER_POLICY_VERSION,
) -> GoogleJoinResult:
    """Join derived Google-challenger evidence onto ``rows`` (J1-J6).

    ``rows`` is the same historical-row shape
    ``research_walkforward.load_research_cases`` consumes. Returns a NEW
    tuple of rows (never mutates a caller's mapping); any row whose
    source-context group matched a vintage-coherent snapshot carries the
    four ``GOOGLE_JOIN_ATTACHED_KEYS`` in addition to its original keys.
    """

    row_hashes, contexts, groups = _group_rows_by_source_context(rows)

    attachments: dict[str, dict[str, object]] = {}
    skips: list[GoogleJoinSkip] = []
    snapshot_cache: dict[tuple[str, str], sqlite3.Row | None] = {}

    for source_hash, indices in groups.items():
        context = contexts[source_hash]
        station_id = str(context["station_id"])
        target_date = str(context["target_date"])

        min_decision_at = _min_decision_at(rows, indices)
        if min_decision_at is None:
            # No row in this group has a parseable decision_at at all --
            # load_research_cases will independently skip every one of
            # them (invalid_decision_at); there is no point-in-time bound
            # to join against here, so this group is left untouched.
            continue

        cache_key = (station_id, target_date)
        if cache_key not in snapshot_cache:
            snapshot_cache[cache_key] = None
        best = _best_snapshot_at_or_before(
            conn,
            station_id=station_id,
            target_date=target_date,
            policy_version=policy_version,
            at_or_before=min_decision_at,
        )
        if best is None:
            continue  # J4: no match -> omit all four keys entirely.

        group_baseline_mu = _finite_float(rows[indices[0]].get("baseline_mu"))
        group_baseline_sigma = _finite_float(rows[indices[0]].get("baseline_sigma"))
        snapshot_baseline_mu = _finite_float(best["baseline_mu"])
        snapshot_baseline_sigma = _finite_float(best["baseline_sigma"])
        vintage_matches = (
            group_baseline_mu is not None
            and group_baseline_sigma is not None
            and snapshot_baseline_mu is not None
            and snapshot_baseline_sigma is not None
            and _floats_close(group_baseline_mu, snapshot_baseline_mu)
            and _floats_close(group_baseline_sigma, snapshot_baseline_sigma)
        )
        if not vintage_matches:
            # J5: vintage mismatch -- attach nothing, record why.
            skips.append(
                GoogleJoinSkip(
                    source_context_hash=source_hash,
                    station_id=station_id,
                    target_date=target_date,
                    reason=GOOGLE_JOIN_REASON_VINTAGE_MISMATCH,
                )
            )
            continue

        challenger_mu = best["challenger_mu"]
        attachments[source_hash] = {
            "google_challenger_action": best["action"],
            "google_challenger_mu": (
                float(challenger_mu) if challenger_mu is not None else None
            ),
            "google_challenger_sigma": float(best["challenger_sigma"]),
            "google_challenger_policy_version": policy_version,
        }

    new_rows: list[dict[str, object]] = []
    matched_row_count = 0
    for index, row in enumerate(rows):
        source_hash = row_hashes[index]
        extra = attachments.get(source_hash) if source_hash is not None else None
        if extra is None:
            new_rows.append(dict(row))
            continue
        matched_row_count += 1
        merged = dict(row)
        merged.update(extra)
        new_rows.append(merged)

    skips.sort(key=lambda skip: (skip.station_id, skip.target_date, skip.source_context_hash))
    return GoogleJoinResult(
        rows=tuple(new_rows), matched_row_count=matched_row_count, skips=tuple(skips)
    )
