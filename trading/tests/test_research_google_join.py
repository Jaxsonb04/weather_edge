"""Task 7: binding Google-evidence join contract (J1-J6).

Probes ``research_google_join.attach_google_challenger_evidence`` against a
real ``PaperStore``-backed sqlite connection (J1: the same paper DB
``scan_context_snapshots`` lives in) so every test exercises the real
``google_challenger_snapshots`` schema/constraints, not a hand-rolled table.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import GoogleChallengerSnapshot
from sfo_kalshi_quant.research_google_join import (
    GOOGLE_JOIN_ATTACHED_KEYS,
    GOOGLE_JOIN_REASON_VINTAGE_MISMATCH,
    GoogleJoinSkip,
    attach_google_challenger_evidence,
)
from sfo_kalshi_quant.research_walkforward import GOOGLE_CHALLENGER_POLICY_VERSION

STATION = "KSFO"
TARGET_DATE = "2026-06-25"


def _forecast_payload(predicted_high_f: float = 66.0) -> dict[str, object]:
    return {"target_date": TARGET_DATE, "predicted_high_f": predicted_high_f}


def _market_payload(ticker: str = "KXHIGHTSFO-TEST-B65.5") -> dict[str, object]:
    return {
        ticker: {
            "ticker": ticker,
            "yes_bid": 0.24,
            "yes_ask": 0.26,
            "floor_strike": 65.0,
            "cap_strike": 66.0,
        }
    }


def _row(
    *,
    station_id: str = STATION,
    target_date: str = TARGET_DATE,
    decision_at: str = "2026-06-25T15:00:00+00:00",
    baseline_mu: float = 66.0,
    baseline_sigma: float = 3.0,
    predicted_high_f: float = 66.0,
) -> dict[str, object]:
    return {
        "target_date": target_date,
        "station_id": station_id,
        "forecast_json": json.dumps(_forecast_payload(predicted_high_f), sort_keys=True),
        "intraday_json": json.dumps({}, sort_keys=True),
        "market_json": json.dumps(_market_payload(), sort_keys=True),
        "prediction_features_json": json.dumps({"predicted_high_f": predicted_high_f}, sort_keys=True),
        "decision_at": decision_at,
        "baseline_mu": baseline_mu,
        "baseline_sigma": baseline_sigma,
    }


def _snapshot(
    *,
    station_id: str = STATION,
    target_date: date = date(2026, 6, 25),
    issued_at: str = "2026-06-25T10:00:00+00:00",
    policy_version: str = GOOGLE_CHALLENGER_POLICY_VERSION,
    baseline_mu: float = 66.0,
    baseline_sigma: float = 3.0,
    challenger_mu: float | None = 67.0,
    challenger_sigma: float = 3.0,
    action: str = "forecast",
) -> GoogleChallengerSnapshot:
    return GoogleChallengerSnapshot(
        station_id=station_id,
        target_date=target_date,
        issued_at=issued_at,
        policy_version=policy_version,
        baseline_mu=baseline_mu,
        baseline_sigma=baseline_sigma,
        challenger_mu=challenger_mu,
        challenger_sigma=challenger_sigma,
        baseline_probabilities={"65.5": 0.4},
        challenger_probabilities={"65.5": 0.5} if challenger_mu is not None else None,
        action=action,
    )


@pytest.fixture()
def store(tmp_path) -> PaperStore:
    return PaperStore(tmp_path / "paper.db")


def _attached_keys(row: dict[str, object]) -> set[str]:
    return set(row) & set(GOOGLE_JOIN_ATTACHED_KEYS)


# ---------------------------------------------------------------------------
# J1-J3: a vintage-coherent, point-in-time-eligible match attaches all four
# keys with the correct column mapping.
# ---------------------------------------------------------------------------


def test_matched_row_gets_all_four_google_keys_attached(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot())
    row = _row()
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    assert result.matched_row_count == 1
    assert result.skips == ()
    (joined,) = result.rows
    assert joined["google_challenger_action"] == "forecast"
    assert joined["google_challenger_mu"] == 67.0
    assert joined["google_challenger_sigma"] == 3.0
    assert joined["google_challenger_policy_version"] == GOOGLE_CHALLENGER_POLICY_VERSION


def test_matched_row_never_overwrites_its_own_baseline_mu_or_sigma(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot(baseline_mu=66.0, baseline_sigma=3.0))
    row = _row(baseline_mu=66.0, baseline_sigma=3.0)
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    (joined,) = result.rows
    assert joined["baseline_mu"] == 66.0
    assert joined["baseline_sigma"] == 3.0


def test_blocked_action_snapshot_attaches_none_challenger_mu(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(
        _snapshot(action="external_runtime_corroboration_block", challenger_mu=None, challenger_sigma=3.0)
    )
    row = _row()
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    (joined,) = result.rows
    assert joined["google_challenger_action"] == "external_runtime_corroboration_block"
    assert joined["google_challenger_mu"] is None
    assert joined["google_challenger_sigma"] == 3.0


# ---------------------------------------------------------------------------
# J4: no match (including a late-issued snapshot) omits all four keys.
# ---------------------------------------------------------------------------


def test_no_matching_snapshot_omits_all_four_keys(store: PaperStore) -> None:
    row = _row()
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    assert result.matched_row_count == 0
    assert result.skips == ()
    (joined,) = result.rows
    assert _attached_keys(joined) == set()
    assert joined == row


def test_late_issued_snapshot_is_rejected_not_matched(store: PaperStore) -> None:
    # Issued AFTER the row's own decision_at -- the point-in-time guard
    # must reject it even though it is otherwise a perfectly valid,
    # vintage-coherent snapshot.
    store.record_google_challenger_snapshot(
        _snapshot(issued_at="2026-06-25T16:00:00+00:00")
    )
    row = _row(decision_at="2026-06-25T15:00:00+00:00")
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    assert result.matched_row_count == 0
    assert result.skips == ()
    (joined,) = result.rows
    assert _attached_keys(joined) == set()


def test_snapshot_for_a_different_station_never_matches(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot(station_id="KLAX"))
    row = _row(station_id=STATION)
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    assert result.matched_row_count == 0
    (joined,) = result.rows
    assert _attached_keys(joined) == set()


def test_snapshot_for_a_different_policy_version_never_matches(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot(policy_version="google-runtime-fixed-v2"))
    row = _row()
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    assert result.matched_row_count == 0
    (joined,) = result.rows
    assert _attached_keys(joined) == set()


# ---------------------------------------------------------------------------
# J5: vintage coherence.
# ---------------------------------------------------------------------------


def test_vintage_mismatch_attaches_nothing_and_records_skip(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot(baseline_mu=66.0, baseline_sigma=3.0))
    row = _row(baseline_mu=70.0, baseline_sigma=3.0)  # different baseline_mu
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    assert result.matched_row_count == 0
    (joined,) = result.rows
    assert _attached_keys(joined) == set()
    assert len(result.skips) == 1
    skip = result.skips[0]
    assert isinstance(skip, GoogleJoinSkip)
    assert skip.reason == GOOGLE_JOIN_REASON_VINTAGE_MISMATCH
    assert skip.station_id == STATION
    assert skip.target_date == TARGET_DATE


def test_vintage_mismatch_on_sigma_alone_also_blocks(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot(baseline_mu=66.0, baseline_sigma=3.0))
    row = _row(baseline_mu=66.0, baseline_sigma=5.0)  # different baseline_sigma
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    assert result.matched_row_count == 0
    assert len(result.skips) == 1
    assert result.skips[0].reason == GOOGLE_JOIN_REASON_VINTAGE_MISMATCH


# ---------------------------------------------------------------------------
# J2: group-level (post-dedup) join, MIN(decision_at) of the whole group.
# ---------------------------------------------------------------------------


def test_join_uses_min_decision_at_across_the_whole_duplicate_group(store: PaperStore) -> None:
    # Two rows for the SAME real-world scan (identical forecast/market/
    # features/target/station -> same source_context_hash), differing
    # only in decision_at (as multiple risk-profile scans of the same
    # cycle legitimately do). A snapshot issued strictly between the two
    # decision_at values must be rejected for BOTH rows -- the join bound
    # is the group's MIN, never a per-row bound.
    earlier = _row(decision_at="2026-06-25T15:00:00+00:00")
    later = _row(decision_at="2026-06-25T15:00:05+00:00")
    store.record_google_challenger_snapshot(
        _snapshot(issued_at="2026-06-25T15:00:02+00:00")
    )
    with store.connect() as conn:
        result = attach_google_challenger_evidence([earlier, later], conn)

    assert result.matched_row_count == 0
    for joined in result.rows:
        assert _attached_keys(joined) == set()


def test_join_applies_identically_to_every_row_in_a_matched_group(store: PaperStore) -> None:
    earlier = _row(decision_at="2026-06-25T15:00:00+00:00")
    later = _row(decision_at="2026-06-25T15:00:05+00:00")
    store.record_google_challenger_snapshot(
        _snapshot(issued_at="2026-06-25T14:00:00+00:00")
    )
    with store.connect() as conn:
        result = attach_google_challenger_evidence([earlier, later], conn)

    assert result.matched_row_count == 2
    first, second = result.rows
    assert _attached_keys(first) == set(GOOGLE_JOIN_ATTACHED_KEYS)
    assert _attached_keys(second) == set(GOOGLE_JOIN_ATTACHED_KEYS)
    assert first["google_challenger_mu"] == second["google_challenger_mu"]


# ---------------------------------------------------------------------------
# Pick MAX(issued_at) among every eligible (<= MIN decision_at) snapshot.
# ---------------------------------------------------------------------------


def test_picks_the_latest_eligible_snapshot_among_several(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(
        _snapshot(issued_at="2026-06-25T08:00:00+00:00", challenger_mu=60.0)
    )
    store.record_google_challenger_snapshot(
        _snapshot(issued_at="2026-06-25T12:00:00+00:00", challenger_mu=68.0)
    )
    # Issued after decision_at -- must never be picked over the two above.
    store.record_google_challenger_snapshot(
        _snapshot(issued_at="2026-06-25T20:00:00+00:00", challenger_mu=99.0)
    )
    row = _row(decision_at="2026-06-25T15:00:00+00:00")
    with store.connect() as conn:
        result = attach_google_challenger_evidence([row], conn)

    (joined,) = result.rows
    assert joined["google_challenger_mu"] == 68.0


# ---------------------------------------------------------------------------
# Structural: a row's attached keys are always the full four-key set, or
# entirely absent -- never a partial set (J4).
# ---------------------------------------------------------------------------


def test_attached_keys_are_never_a_partial_set(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot())
    matched_row = _row(decision_at="2026-06-25T15:00:00+00:00")
    unmatched_row = _row(
        target_date="2026-06-26",
        decision_at="2026-06-26T15:00:00+00:00",
        predicted_high_f=70.0,
    )
    with store.connect() as conn:
        result = attach_google_challenger_evidence([matched_row, unmatched_row], conn)

    for joined in result.rows:
        assert _attached_keys(joined) in (set(), set(GOOGLE_JOIN_ATTACHED_KEYS))


# ---------------------------------------------------------------------------
# Determinism and immutability.
# ---------------------------------------------------------------------------


def test_join_is_deterministic(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot())
    row = _row()
    with store.connect() as conn:
        first = attach_google_challenger_evidence([row], conn)
        second = attach_google_challenger_evidence([row], conn)

    assert first.rows == second.rows
    assert first.matched_row_count == second.matched_row_count


def test_join_never_mutates_the_caller_supplied_row(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot())
    row = _row()
    original = dict(row)
    with store.connect() as conn:
        attach_google_challenger_evidence([row], conn)

    assert row == original


def test_a_row_that_cannot_be_canonicalized_is_passed_through_unchanged(store: PaperStore) -> None:
    store.record_google_challenger_snapshot(_snapshot())
    malformed_row = {"target_date": TARGET_DATE, "station_id": STATION}  # no forecast/market/features
    with store.connect() as conn:
        result = attach_google_challenger_evidence([malformed_row], conn)

    assert result.matched_row_count == 0
    assert result.rows == (malformed_row,)
