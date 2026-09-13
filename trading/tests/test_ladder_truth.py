"""The full-ladder outcome ledger (audit IMP-1).

``market_day_settlements`` records only market-days the book traded -- about
nineteen rows -- so every calibration statement in this project is conditioned
on the trade filter that is itself under suspicion.  ``ladder_bin_outcomes``
resolves EVERY offered bin against the same final NWS CLI maximum the exchange
settles on, which is ~100x the evidence for zero extra data collection.

These tests pin the three things that can silently corrupt that ledger: the bin
boundary rule, the station-standard day-ahead cutoff, and the integrity guard
that is supposed to make a bad CLI value loud instead of invisible.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.ladder_truth import (
    INTEGRITY_FLAGGED,
    INTEGRITY_OK,
    INTEGRITY_SOURCE_EXCHANGE,
    INTEGRITY_SOURCE_OBSERVED,
    INTEGRITY_UNCHECKED,
    LADDER_INTEGRITY_TOLERANCE_F,
    LADDER_OBSERVED_MIN_OBSERVATIONS,
    LADDER_OBSERVED_TOLERANCE_F,
    LADDER_RETENTION_FULL_DAYS,
    QUOTE_LEAD_DAY_AHEAD,
    QUOTE_LEAD_SAME_DAY,
    TRUTH_SOURCE_CLI_SETTLEMENT,
    assess_integrity,
    assess_ladder_coverage,
    day_ahead_cutoff_utc,
    derive_integrity_verdict,
    exchange_settlement_highs,
    offered_ladder_quotes,
    previous_complete_settlement_day,
    score_ladder_outcomes,
    settlement_day_close_utc,
    side_wins,
    target_dates_in_range,
)
from sfo_kalshi_quant.settlement_truth import bin_resolves_yes, integer_settlement_high_f
from sfo_kalshi_quant.store.schema import _LADDER_INTEGRITY_MIGRATION_KEY

SFO = get_city("sfo")
MIA = get_city("mia")

_DECISION_COLUMNS = (
    "created_at, target_date, market_ticker, label, action, side, approved, "
    "probability, probability_lcb, model_probability, market_probability, "
    "yes_bid, yes_ask, entry_bid, entry_ask, spread, fee_per_contract, "
    "cost_per_contract, edge, edge_lcb, kelly_fraction, recommended_contracts, "
    "recommended_spend, expected_profit, trade_quality_score, strike_type, "
    "floor_strike, cap_strike, risk_profile, reasons_json"
)


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    created_at: str,
    target_date: str,
    ticker: str,
    floor: float | None,
    cap: float | None,
    strike_type: str = "between",
    side: str = "NO",
    model_probability: float = 0.8,
    market_probability: float = 0.85,
    entry_bid: float = 0.84,
    entry_ask: float = 0.86,
    risk_profile: str = "research",
) -> int:
    cursor = conn.execute(
        f"INSERT INTO decision_snapshots ({_DECISION_COLUMNS}) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)",
        (
            created_at, target_date, ticker, "label", "BUY_NO", side, 1,
            model_probability, model_probability - 0.05, model_probability,
            market_probability, 0.14, 0.16, entry_bid, entry_ask, 0.02, 0.006,
            entry_ask + 0.006, 0.05, 0.02, 0.01, 1.0, 1.0, 0.1, 0.5,
            strike_type, floor, cap, risk_profile, "[]",
        ),
    )
    return int(cursor.lastrowid)


def _store(tmp: str) -> PaperStore:
    return PaperStore(Path(tmp) / "paper.db")


# ---------------------------------------------------------------------------
# The bin boundary rule -- the whole label depends on it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strike_type, floor, cap, high, expected",
    [
        # A `between` bin is closed on BOTH edges: the exchange's 66-67 bin wins
        # at exactly 66 and at exactly 67, and the ladder tiles the line only if
        # that stays true.
        ("between", 66.0, 67.0, 66.0, True),
        ("between", 66.0, 67.0, 67.0, True),
        ("between", 66.0, 67.0, 65.0, False),
        ("between", 66.0, 67.0, 68.0, False),
        # `less` is strict: the T88 cap loses at exactly 88.
        ("less", None, 88.0, 87.0, True),
        ("less", None, 88.0, 88.0, False),
        ("less", None, 88.0, 89.0, False),
        # `greater` is strict the other way: the T95 floor loses at exactly 95.
        ("greater", 95.0, None, 96.0, True),
        ("greater", 95.0, None, 95.0, False),
        ("greater", 95.0, None, 94.0, False),
    ],
)
def test_bin_boundaries_are_closed_between_and_strict_outside(
    strike_type, floor, cap, high, expected
):
    assert bin_resolves_yes(strike_type, floor, cap, float(high)) is expected


@pytest.mark.parametrize(
    "raw, expected",
    [(66.0, 66.0), (66.4, 66.0), (66.5, 67.0), (66.6, 67.0), (-1.5, -1.0)],
)
def test_settlement_high_is_the_integer_the_market_settles_on(raw, expected):
    """CLI values are integers; anything else rounds before it can touch a bin."""

    assert integer_settlement_high_f(raw) == expected


def test_an_integer_cli_value_lands_cleanly_on_the_bin_it_names():
    """The common case: CLI says 67, the 66-67 bin wins and its neighbours lose."""

    high = integer_settlement_high_f(67)
    assert bin_resolves_yes("between", 66.0, 67.0, high) is True
    assert bin_resolves_yes("between", 68.0, 69.0, high) is False
    assert bin_resolves_yes("less", None, 66.0, high) is False
    assert bin_resolves_yes("greater", 69.0, None, high) is False


def test_side_won_is_not_the_bin_outcome():
    """Conflating the two is the defect `_migrate_closed_row_position_won` fixed."""

    assert side_wins("YES", True) is True
    assert side_wins("YES", False) is False
    assert side_wins("NO", True) is False
    assert side_wins("NO", False) is True


# ---------------------------------------------------------------------------
# The integrity guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cli, exchange, status, delta",
    [
        (90.0, 90.0, INTEGRITY_OK, 0.0),
        (91.0, 90.0, INTEGRITY_OK, 1.0),
        (89.0, 90.0, INTEGRITY_OK, -1.0),
        # 2 F is the audit's hard line, and it is inclusive.
        (92.0, 90.0, INTEGRITY_FLAGGED, 2.0),
        (88.0, 90.0, INTEGRITY_FLAGGED, -2.0),
        # The measured failure: KMIA 2026-08-29, a 5 F CLI outlier.
        (85.0, 90.0, INTEGRITY_FLAGGED, -5.0),
        # No exchange record is not agreement.
        (90.0, None, INTEGRITY_UNCHECKED, None),
    ],
)
def test_integrity_guard_flags_at_two_degrees_and_never_assumes_agreement(
    cli, exchange, status, delta
):
    expected_source = None if exchange is None else INTEGRITY_SOURCE_EXCHANGE
    assert assess_integrity(cli, exchange) == (status, delta, expected_source)


def test_integrity_tolerance_is_the_documented_two_degrees():
    assert LADDER_INTEGRITY_TOLERANCE_F == 2.0


def test_the_observation_tape_is_the_channel_that_covers_the_scored_era():
    """The exchange channel is structurally inert where we actually measure.

    Finalized ``dataset_kalshi_markets`` rows stop at target date 2026-07-07
    while the scored window starts weeks later, so a guard built on that source
    alone reports ``0 flagged`` over 2,700 rows it never looked at -- worse than
    no guard, because the zero reads as agreement. The station's own observation
    tape covers every era the journal does, at a wider tolerance because it is a
    coarser instrument.
    """

    # The observation tape is used only when the exchange has nothing to say.
    assert assess_integrity(90.0, 90.0, 84.0) == (INTEGRITY_OK, 0.0, INTEGRITY_SOURCE_EXCHANGE)
    # And when it is the only channel, it decides -- at its own threshold.
    assert assess_integrity(92.0, None, 90.0) == (
        INTEGRITY_OK, 2.0, INTEGRITY_SOURCE_OBSERVED,
    )
    assert assess_integrity(93.0, None, 90.0) == (
        INTEGRITY_FLAGGED, 3.0, INTEGRITY_SOURCE_OBSERVED,
    )
    # Neither channel is still `unchecked`, never a silent `ok`.
    assert assess_integrity(90.0, None, None) == (INTEGRITY_UNCHECKED, None, None)


def test_the_observed_tolerance_is_wider_than_the_exchange_tolerance():
    """Measured, not assumed: over the 863 dense-tape station-days from
    2026-06-01, |CLI - observed| never exceeded 2 F, so 3 F flags 0 of 863 while
    still catching the multi-degree parse error the guard exists for. The
    exchange value is the number that moved money and needs no such headroom."""

    assert LADDER_OBSERVED_TOLERANCE_F > LADDER_INTEGRITY_TOLERANCE_F
    assert LADDER_OBSERVED_TOLERANCE_F == 3.0
    assert LADDER_OBSERVED_MIN_OBSERVATIONS == 200


def test_derive_integrity_verdict_matches_the_live_assessment():
    assert derive_integrity_verdict(92.0, 90.0) == (
        INTEGRITY_FLAGGED, 2.0, INTEGRITY_SOURCE_EXCHANGE,
    )
    assert derive_integrity_verdict(91.0, 90.0) == (
        INTEGRITY_OK, 1.0, INTEGRITY_SOURCE_EXCHANGE,
    )
    assert derive_integrity_verdict(94.0, None, 90.0) == (
        INTEGRITY_FLAGGED, 4.0, INTEGRITY_SOURCE_OBSERVED,
    )
    assert derive_integrity_verdict(90.0, None) == (INTEGRITY_UNCHECKED, None, None)
    assert derive_integrity_verdict(None, 90.0) == (INTEGRITY_UNCHECKED, None, None)


def test_a_contradicting_exchange_record_is_dropped_not_silently_collapsed():
    """A city-day carrying two settlement values is a contradiction inside the
    guard's own reference, so it cannot be a second opinion about anything.
    Keeping whichever row SQLite returned first is the swallow-the-contradiction
    defect this module exists to refuse."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE dataset_kalshi_markets (ticker TEXT PRIMARY KEY, "
        "target_date TEXT, market_status TEXT, expiration_value REAL)"
    )
    conn.executemany(
        "INSERT INTO dataset_kalshi_markets VALUES (?, ?, 'finalized', ?)",
        [
            ("KXHIGHTSFO-26AUG18-B66.5", "2026-08-18", 67.0),
            ("KXHIGHTSFO-26AUG18-T64", "2026-08-18", 72.0),
            ("KXHIGHNY-26AUG18-B80.5", "2026-08-18", 81.0),
        ],
    )
    highs = exchange_settlement_highs(conn)
    assert ("KXHIGHTSFO", "2026-08-18") not in highs
    assert highs[("KXHIGHNY", "2026-08-18")] == 81.0


# ---------------------------------------------------------------------------
# The station-standard day-ahead cutoff
# ---------------------------------------------------------------------------


def test_day_ahead_cutoff_uses_fixed_standard_time_not_the_civil_dst_day():
    """The NWS climate day is standard time year round (see settlement_day).

    In August the Pacific civil day opens at 07:00 UTC; the settlement day opens
    at 08:00 UTC. A civil cutoff would let an hour of the target day's own
    quotes in and call them day-ahead.
    """

    assert day_ahead_cutoff_utc(SFO, date(2026, 8, 18)) == "2026-08-18T08:00:00+00:00"
    assert day_ahead_cutoff_utc(MIA, date(2026, 8, 18)) == "2026-08-18T05:00:00+00:00"
    # Winter, when civil and standard agree, must give the same answer.
    assert day_ahead_cutoff_utc(SFO, date(2026, 1, 18)) == "2026-01-18T08:00:00+00:00"


def test_nightly_day_is_the_previous_day_on_the_slowest_station():
    """A day is only complete once it has closed at the westernmost station."""

    # 2026-08-19T06:00Z is already Aug 19 in Miami but still Aug 18 at SFO.
    now = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)
    assert previous_complete_settlement_day(now) == "2026-08-17"
    later = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)
    assert previous_complete_settlement_day(later) == "2026-08-18"


def test_target_dates_in_range_is_inclusive_and_rejects_inverted_ranges():
    assert target_dates_in_range("2026-08-18", "2026-08-20") == [
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
    ]
    assert target_dates_in_range("2026-08-18", "2026-08-18") == ["2026-08-18"]
    with pytest.raises(ValueError, match="precedes start"):
        target_dates_in_range("2026-08-20", "2026-08-18")


# ---------------------------------------------------------------------------
# The ledger itself
# ---------------------------------------------------------------------------


def _seed_one_sfo_day(conn: sqlite3.Connection) -> dict[str, int]:
    """A three-bin SFO ladder for 2026-08-18, quoted twice day-ahead and once
    during the target day."""

    ids: dict[str, int] = {}
    bins = (
        ("KXHIGHTSFO-26AUG18-B66.5", "between", 66.0, 67.0),
        ("KXHIGHTSFO-26AUG18-T64", "less", None, 64.0),
        ("KXHIGHTSFO-26AUG18-T70", "greater", 70.0, None),
    )
    for ticker, strike_type, floor, cap in bins:
        _insert_decision(
            conn, created_at="2026-08-17T18:00:00+00:00", target_date="2026-08-18",
            ticker=ticker, floor=floor, cap=cap, strike_type=strike_type,
            model_probability=0.60, market_probability=0.61,
        )
        ids[ticker] = _insert_decision(
            conn, created_at="2026-08-18T07:59:00+00:00", target_date="2026-08-18",
            ticker=ticker, floor=floor, cap=cap, strike_type=strike_type,
            model_probability=0.70, market_probability=0.75,
            entry_bid=0.74, entry_ask=0.76,
        )
        # After 08:00Z the SFO settlement day has opened: same-day, must not win.
        _insert_decision(
            conn, created_at="2026-08-18T20:00:00+00:00", target_date="2026-08-18",
            ticker=ticker, floor=floor, cap=cap, strike_type=strike_type,
            model_probability=0.95, market_probability=0.99,
        )
    return ids


def test_every_offered_bin_is_scored_not_just_the_traded_one():
    """The point of IMP-1: the ledger is not conditioned on the trade filter."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            chosen = _seed_one_sfo_day(conn)

        summary = store.backfill_ladder_bin_outcomes(
            start="2026-08-18",
            end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        assert summary["offered_bins"] == 3
        assert summary["resolved_bins"] == 3
        assert summary["recorded_bins"] == 3
        assert summary["missing_truth_bins"] == 0

        rows = {str(row["market_ticker"]): row for row in store.ladder_bin_outcomes()}
        assert set(rows) == set(chosen)

        # 67 lands inside 66-67, below the T70 floor and at/above the T64 cap.
        assert rows["KXHIGHTSFO-26AUG18-B66.5"]["resolved_yes"] == 1
        assert rows["KXHIGHTSFO-26AUG18-T64"]["resolved_yes"] == 0
        assert rows["KXHIGHTSFO-26AUG18-T70"]["resolved_yes"] == 0
        # The book quoted NO, so it wins wherever the bin loses.
        assert rows["KXHIGHTSFO-26AUG18-B66.5"]["side_won"] == 0
        assert rows["KXHIGHTSFO-26AUG18-T64"]["side_won"] == 1
        assert rows["KXHIGHTSFO-26AUG18-T70"]["side_won"] == 1

        for ticker, row in rows.items():
            assert row["settlement_high_f"] == 67.0
            assert row["truth_source"] == TRUTH_SOURCE_CLI_SETTLEMENT
            assert row["series_ticker"] == "KXHIGHTSFO"
            assert row["station_id"] == "KSFO"
            assert row["side"] == "NO"
            assert row["traded"] == 0
            # The last DAY-AHEAD quote wins, never the far better same-day one.
            assert row["quote_lead"] == QUOTE_LEAD_DAY_AHEAD
            assert row["quote_snapshot_id"] == chosen[ticker]
            assert row["quote_created_at"] == "2026-08-18T07:59:00+00:00"
            assert row["market_probability"] == pytest.approx(0.75)
            assert row["model_probability"] == pytest.approx(0.70)
            assert row["entry_bid"] == pytest.approx(0.74)
            assert row["snapshot_count"] == 2
            assert row["quote_risk_profile"] == "research"
            # No exchange record for this day: unchecked, never a silent "ok".
            assert row["integrity_status"] == INTEGRITY_UNCHECKED
            assert row["truth_delta_f"] is None


def test_a_bin_only_ever_quoted_on_the_day_is_kept_and_labelled_same_day():
    """The ladder stays complete, but the easier quote class stays separable."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _insert_decision(
                conn, created_at="2026-08-18T20:00:00+00:00", target_date="2026-08-18",
                ticker="KXHIGHTSFO-26AUG18-B80.5", floor=80.0, cap=81.0,
            )
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        row = store.ladder_bin_outcomes()[0]
        assert row["quote_lead"] == QUOTE_LEAD_SAME_DAY
        assert row["quote_created_at"] == "2026-08-18T20:00:00+00:00"


def test_a_station_day_without_final_cli_truth_is_reported_never_guessed():
    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
        summary = store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs={}
        )
        assert summary["resolved_bins"] == 0
        assert summary["missing_truth_bins"] == 3
        assert {entry["station_id"] for entry in summary["missing_truth"]} == {"KSFO"}
        assert store.ladder_bin_outcomes() == []


def test_dry_run_writes_nothing():
    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
        summary = store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
            dry_run=True,
        )
        assert summary["resolved_bins"] == 3
        assert summary["recorded_bins"] == 0
        assert store.ladder_bin_outcomes() == []


def test_rerunning_is_idempotent_and_keeps_the_first_sighting():
    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
        highs = {("KXHIGHTSFO", "2026-08-18"): 67.0}
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs
        )
        first = {str(r["market_ticker"]): dict(r) for r in store.ladder_bin_outcomes()}
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs
        )
        second = {str(r["market_ticker"]): dict(r) for r in store.ladder_bin_outcomes()}
        assert set(second) == set(first)
        for ticker, row in second.items():
            assert row["recorded_at"] == first[ticker]["recorded_at"]
            assert row["resolved_yes"] == first[ticker]["resolved_yes"]


def test_the_traded_flag_marks_the_bins_the_old_ledger_could_see():
    """The traded subset is a column here, not the population."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
            conn.execute(
                "INSERT INTO paper_orders (created_at, target_date, market_ticker, "
                "label, action, side, contracts, yes_ask, fee_per_contract, "
                "cost_per_contract, probability, probability_lcb, edge, edge_lcb, "
                "expected_profit, trade_quality_score, status, reasons_json) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-08-17T18:05:00+00:00", "2026-08-18",
                    "KXHIGHTSFO-26AUG18-T64", "label", "BUY_NO", "NO", 1.0, 0.16,
                    0.006, 0.166, 0.8, 0.7, 0.05, 0.02, 0.1, 0.5,
                    "PAPER_FILLED", "[]",
                ),
            )
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        rows = {str(r["market_ticker"]): r for r in store.ladder_bin_outcomes()}
        assert rows["KXHIGHTSFO-26AUG18-T64"]["traded"] == 1
        assert rows["KXHIGHTSFO-26AUG18-B66.5"]["traded"] == 0
        # There are two economically separate paper books. A bin held only by
        # the research sleeve is not the same evidence as one the live book
        # held, so the flag alone cannot be the whole answer.
        # A row with no profile is the live book, matching
        # _paper_profile_filter's own COALESCE(risk_profile, 'live').
        assert rows["KXHIGHTSFO-26AUG18-T64"]["traded_profiles"] == "live"
        assert rows["KXHIGHTSFO-26AUG18-B66.5"]["traded_profiles"] is None


def test_a_disagreeing_exchange_settlement_is_flagged_not_absorbed():
    """A 5 F CLI outlier must be loud. Silently labelling every bin off it is
    exactly the contamination the audit warned about."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
            conn.execute(
                "CREATE TABLE dataset_kalshi_markets (ticker TEXT PRIMARY KEY, "
                "target_date TEXT, market_status TEXT, expiration_value REAL)"
            )
            conn.execute(
                "INSERT INTO dataset_kalshi_markets VALUES (?, ?, 'finalized', ?)",
                ("KXHIGHTSFO-26AUG18-B66.5", "2026-08-18", 72.0),
            )
        summary = store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        assert len(summary["flagged"]) == 3
        assert summary["flagged"][0]["truth_delta_f"] == pytest.approx(-5.0)
        assert summary["flagged"][0]["integrity_source"] == INTEGRITY_SOURCE_EXCHANGE
        rows = store.ladder_bin_outcomes()
        assert {str(row["integrity_status"]) for row in rows} == {INTEGRITY_FLAGGED}
        # Flagged rows are recorded (the evidence must survive) but excluded
        # from every calibration number.
        score = store.score_ladder_bin_outcomes()
        assert score["flagged_bins_excluded"] == 3
        assert score["scored_bins"] == 0
        assert score["brier_market"] is None


def test_an_agreeing_exchange_settlement_passes_the_guard():
    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
            conn.execute(
                "CREATE TABLE dataset_kalshi_markets (ticker TEXT PRIMARY KEY, "
                "target_date TEXT, market_status TEXT, expiration_value REAL)"
            )
            conn.execute(
                "INSERT INTO dataset_kalshi_markets VALUES (?, ?, 'finalized', ?)",
                ("KXHIGHTSFO-26AUG18-B66.5", "2026-08-18", 67.0),
            )
        summary = store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        assert summary["flagged"] == []
        rows = store.ladder_bin_outcomes()
        assert {str(row["integrity_status"]) for row in rows} == {INTEGRITY_OK}
        assert {row["truth_delta_f"] for row in rows} == {0.0}


def test_scoring_separates_model_from_market_on_the_recorded_side():
    """A perfectly-called market and a wrong model must not average away."""

    rows = [
        {
            "integrity_status": INTEGRITY_OK, "side_won": 1, "traded": 0,
            "market_ticker": "A", "target_date": "2026-08-18",
            "series_ticker": "KXHIGHTSFO",
            "model_probability": 0.5, "market_probability": 1.0,
        },
        {
            "integrity_status": INTEGRITY_OK, "side_won": 0, "traded": 1,
            "market_ticker": "B", "target_date": "2026-08-18",
            "series_ticker": "KXHIGHTSFO",
            "model_probability": 0.5, "market_probability": 0.0,
        },
    ]
    score = score_ladder_outcomes(rows)
    assert score["scored_bins"] == 2
    assert score["day_markets"] == 2
    assert score["traded_bins"] == 1
    assert score["brier_market"] == pytest.approx(0.0)
    assert score["brier_model"] == pytest.approx(0.25)
    assert score["brier_blend"] == pytest.approx(0.0625)
    assert score["realized_frequency"] == pytest.approx(0.5)


def test_scoring_a_side_filter_keeps_complementary_rows_apart():
    """YES and NO rows on one bin have exactly complementary outcomes; pooling
    them pins realized frequency at 0.5 and destroys the comparison."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            for side, market_p in (("NO", 0.9), ("YES", 0.1)):
                _insert_decision(
                    conn, created_at="2026-08-17T18:00:00+00:00",
                    target_date="2026-08-18", ticker="KXHIGHTSFO-26AUG18-B80.5",
                    floor=80.0, cap=81.0, side=side, market_probability=market_p,
                    model_probability=market_p,
                )
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        assert len(store.ladder_bin_outcomes()) == 2
        assert store.score_ladder_bin_outcomes()["realized_frequency"] == pytest.approx(0.5)
        no_side = store.score_ladder_bin_outcomes(side="NO")
        assert no_side["scored_bins"] == 1
        assert no_side["realized_frequency"] == pytest.approx(1.0)
        assert no_side["brier_market"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def test_migration_re_derives_stale_integrity_verdicts_exactly_once():
    """``integrity_status`` is a cached projection of the tolerance constant.

    A row written under a different tolerance carries a verdict that no longer
    means what the constant says, and a stale ``ok`` is a silently accepted bad
    label -- the one failure the guard exists to prevent.
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "paper.db"
        store = PaperStore(path)
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO ladder_bin_outcomes (market_ticker, target_date, side, "
                "series_ticker, station_id, recorded_at, updated_at, quote_lead, "
                "quote_created_at, snapshot_count, settlement_high_f, truth_source, "
                "resolved_yes, side_won, traded, exchange_settlement_high_f, "
                "truth_delta_f, integrity_status) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "KXHIGHTSFO-26AUG18-B66.5", "2026-08-18", "NO", "KXHIGHTSFO",
                    "KSFO", "t", "t", QUOTE_LEAD_DAY_AHEAD, "t", 1, 72.0,
                    TRUTH_SOURCE_CLI_SETTLEMENT, 0, 1, 0, 67.0, 5.0, INTEGRITY_OK,
                ),
            )
            conn.execute(
                "DELETE FROM schema_migrations WHERE migration_key = ?",
                (_LADDER_INTEGRITY_MIGRATION_KEY,),
            )

        PaperStore(path)  # re-init runs the migration

        with store.connect() as conn:
            status = conn.execute(
                "SELECT integrity_status FROM ladder_bin_outcomes"
            ).fetchone()[0]
            assert status == INTEGRITY_FLAGGED
            assert conn.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_key = ?",
                (_LADDER_INTEGRITY_MIGRATION_KEY,),
            ).fetchone() is not None
            # The verdict block is one projection: status, delta and the channel
            # that produced it all move together.
            assert conn.execute(
                "SELECT integrity_source FROM ladder_bin_outcomes"
            ).fetchone()[0] == INTEGRITY_SOURCE_EXCHANGE
            # It never rewrites an outcome, only the verdict over it.
            row = conn.execute(
                "SELECT settlement_high_f, resolved_yes, side_won, truth_delta_f "
                "FROM ladder_bin_outcomes"
            ).fetchone()
            assert tuple(row) == (72.0, 0, 1, 5.0)

        # Second init is a no-op: the ledger row is untouched.
        with store.connect() as conn:
            conn.execute(
                "UPDATE ladder_bin_outcomes SET integrity_status = ?", (INTEGRITY_OK,)
            )
        PaperStore(path)
        with store.connect() as conn:
            assert conn.execute(
                "SELECT integrity_status FROM ladder_bin_outcomes"
            ).fetchone()[0] == INTEGRITY_OK


def test_the_table_and_its_indexes_exist_on_a_fresh_store():
    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(ladder_bin_outcomes)")}
            indexes = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'ladder_bin_outcomes'"
                )
            }
    assert {
        "market_ticker", "series_ticker", "target_date", "station_id", "side",
        "strike_type", "floor_strike", "cap_strike", "model_probability",
        "market_probability", "entry_bid", "entry_ask", "settlement_high_f",
        "resolved_yes", "side_won", "truth_source", "recorded_at", "updated_at",
        "traded", "traded_profiles",
        "exchange_settlement_high_f", "observed_settlement_high_f",
        "truth_delta_f", "integrity_source", "integrity_status",
    } <= columns
    assert "idx_ladder_bin_outcomes_target" in indexes
    assert "idx_ladder_bin_outcomes_integrity" in indexes


def test_nothing_in_the_trading_path_reads_the_ladder_ledger():
    """This table measures decisions; it must never make one."""

    package = Path(__file__).resolve().parents[1] / "sfo_kalshi_quant"
    allowed = {
        "ladder_truth.py",     # the ledger itself
        "store/schema.py",     # table creation + the integrity migration
        "db.py",               # backfill + read-back accessors
        "_cli/paper.py",       # operator command
        "_cli/parser.py",      # operator command registration
        "cli.py",              # operator command dispatch
    }
    offenders = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if ("ladder_bin_outcomes" in path.read_text() or "ladder_truth" in path.read_text())
        and str(path.relative_to(package)) not in allowed
    )
    assert offenders == []


def test_the_nightly_unit_ships_uninstalled_and_out_of_the_canonical_set():
    """``deploy/aws/systemd/`` is a closed set: three repository invariants
    require every template directly under it to be installed, deploy-verified
    and alert-wired, so putting one there IS a deploy change. The ladder units
    therefore ship under ``systemd/not-installed/``, which nothing globs and no
    installer renders."""

    aws = Path(__file__).resolve().parents[1] / "deploy" / "aws"
    holding = aws / "systemd" / "not-installed"
    service = holding / "sfo-kalshi-ladder-outcomes.service.in"
    timer = holding / "sfo-kalshi-ladder-outcomes.timer"
    assert service.exists() and timer.exists()
    assert (holding / "README.md").exists()
    assert not list((aws / "systemd").glob("*ladder*"))
    assert "EnvironmentFile=__ENV_FILE__" in service.read_text()
    assert "paper-ladder-outcomes --nightly" in service.read_text()
    # The final CLI for D-1 is issued 01:30-04:40 local (audit FC-11), i.e.
    # 09:30-12:40 UTC for the westernmost UTC-8 station. A nightly inside that
    # window silently loses the Pacific cities on some nights.
    assert "OnCalendar=*-*-* 13:20:00" in timer.read_text()
    for name in (
        "install_systemd.sh",
        "install_systemd_notimers.sh",
        "check_scheduler_health.sh",
        "verify_systemd_unit_integrity.sh",
        "disable_systemd_timers.sh",
    ):
        assert "ladder-outcomes" not in (aws / name).read_text(), name


# ---------------------------------------------------------------------------
# Completeness -- the population, not just the labels
# ---------------------------------------------------------------------------


def test_the_retention_horizon_matches_the_pruner():
    """The horizon is only meaningful if it is the pruner's own number."""

    full_days = inspect.signature(
        PaperStore.prune_decision_snapshots
    ).parameters["full_days"].default
    assert LADDER_RETENTION_FULL_DAYS == full_days


def test_coverage_names_the_city_days_retention_has_already_thinned():
    """Retention deletes exactly the rows this ledger reads
    (``COALESCE(approved,0)=0 AND COALESCE(signal_approved,0)=0``), so a
    backfill over an old window scores only the approval-biased survivors --
    the trade-filter conditioning IMP-1 exists to escape. Without an
    expected-versus-actual check the CLI prints ``offered_bins: 30`` and looks
    fine. On production this separates the eras cleanly: every city-day from
    2026-07-19 carries 6 bins, June and early July carry 1-5."""

    coverage = assess_ladder_coverage(
        {
            ("KXHIGHTSFO", "2026-09-05"): 6,
            ("KXHIGHNY", "2026-09-05"): 6,
            ("KXHIGHTSFO", "2026-06-20"): 2,
        },
        today="2026-09-07",
        full_days=7,
    )
    assert coverage["expected_bins_per_city_day"] == 6
    assert coverage["thin_city_days"] == [
        {
            "series_ticker": "KXHIGHTSFO",
            "target_date": "2026-06-20",
            "offered_bins": 2,
            "expected_bins": 6,
        }
    ]
    # And the horizon is reported even for a city-day whose bin count still
    # looks right: past it the population is approval-biased whatever its size.
    assert coverage["retention_incomplete_dates"] == ["2026-06-20"]
    assert coverage["retention_full_fidelity_since"] == "2026-08-31"


def test_a_complete_recent_ladder_raises_nothing():
    coverage = assess_ladder_coverage(
        {("KXHIGHTSFO", "2026-09-05"): 6, ("KXHIGHNY", "2026-09-05"): 6},
        today="2026-09-07",
        full_days=7,
    )
    assert coverage["thin_city_days"] == []
    assert coverage["retention_incomplete_dates"] == []


def test_the_backfill_reports_coverage_for_the_range_it_read():
    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
        summary = store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
            today="2026-08-19",
        )
        assert summary["coverage"]["city_days"] == 1
        assert summary["coverage"]["expected_bins_per_city_day"] == 3
        assert summary["coverage"]["thin_city_days"] == []
        assert summary["coverage"]["retention_incomplete_dates"] == []
        # The same range read a month later is past the horizon and says so.
        stale = store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
            today="2026-09-30",
        )
        assert stale["coverage"]["retention_incomplete_dates"] == ["2026-08-18"]


def test_a_rerun_on_an_eroded_journal_cannot_downgrade_a_day_ahead_quote():
    """The failure this prevents: retention removes the day-ahead close, the
    same-day row survives, and a re-run silently replaces a correct day_ahead
    quote with a later, far easier same_day one -- with nothing in the row to
    show it happened."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        highs = {("KXHIGHTSFO", "2026-08-18"): 67.0}
        with store.connect() as conn:
            _insert_decision(
                conn, created_at="2026-08-18T07:59:00+00:00",
                target_date="2026-08-18", ticker="KXHIGHTSFO-26AUG18-B66.5",
                floor=66.0, cap=67.0, model_probability=0.70,
                market_probability=0.75,
            )
            _insert_decision(
                conn, created_at="2026-08-18T20:00:00+00:00",
                target_date="2026-08-18", ticker="KXHIGHTSFO-26AUG18-B66.5",
                floor=66.0, cap=67.0, model_probability=0.95,
                market_probability=0.99,
            )
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs
        )
        first = store.ladder_bin_outcomes()[0]
        assert first["quote_lead"] == QUOTE_LEAD_DAY_AHEAD

        # Retention now deletes the unapproved day-ahead row, leaving only the
        # same-day survivor, and the ledger is re-run over the same range.
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM decision_snapshots WHERE created_at = ?",
                ("2026-08-18T07:59:00+00:00",),
            )
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs
        )
        after = store.ladder_bin_outcomes()[0]
        assert after["quote_lead"] == QUOTE_LEAD_DAY_AHEAD
        assert after["quote_created_at"] == "2026-08-18T07:59:00+00:00"
        assert after["market_probability"] == pytest.approx(0.75)


def test_a_later_day_ahead_close_still_replaces_an_earlier_one():
    """The rule is "never degrade", not "never move": a first run that saw only
    an early quote must still take the real day-ahead close when it appears."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        highs = {("KXHIGHTSFO", "2026-08-18"): 67.0}
        with store.connect() as conn:
            _insert_decision(
                conn, created_at="2026-08-17T18:00:00+00:00",
                target_date="2026-08-18", ticker="KXHIGHTSFO-26AUG18-B66.5",
                floor=66.0, cap=67.0, market_probability=0.61,
            )
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs
        )
        with store.connect() as conn:
            _insert_decision(
                conn, created_at="2026-08-18T07:59:00+00:00",
                target_date="2026-08-18", ticker="KXHIGHTSFO-26AUG18-B66.5",
                floor=66.0, cap=67.0, market_probability=0.75,
            )
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs
        )
        row = store.ladder_bin_outcomes()[0]
        assert row["quote_created_at"] == "2026-08-18T07:59:00+00:00"
        assert row["market_probability"] == pytest.approx(0.75)


def test_truth_that_arrives_late_still_refreshes_a_row_whose_quote_is_frozen():
    """The no-downgrade rule guards the QUOTE. The label must keep moving, or a
    row first written as ``unchecked`` would stay unchecked forever."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
        highs = {("KXHIGHTSFO", "2026-08-18"): 67.0}
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs
        )
        assert {
            str(row["integrity_status"]) for row in store.ladder_bin_outcomes()
        } == {INTEGRITY_UNCHECKED}
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18", cli_settlement_highs=highs,
            observed_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        rows = store.ladder_bin_outcomes()
        assert {str(row["integrity_status"]) for row in rows} == {INTEGRITY_OK}
        assert {str(row["integrity_source"]) for row in rows} == {
            INTEGRITY_SOURCE_OBSERVED
        }
        assert {row["observed_settlement_high_f"] for row in rows} == {67.0}
        # ...without disturbing the quote it already chose.
        assert {str(row["quote_lead"]) for row in rows} == {QUOTE_LEAD_DAY_AHEAD}


# ---------------------------------------------------------------------------
# The quote window
# ---------------------------------------------------------------------------


def test_the_same_day_window_closes_when_the_climate_day_closes():
    """A bare ``target_date + N`` date string is up to sixteen hours late for a
    UTC-8 station, so it structurally permits a post-resolution quote to become
    a bin's scored view. The bound is the station's own day close."""

    assert settlement_day_close_utc(SFO, date(2026, 8, 18)) == "2026-08-19T08:00:00+00:00"
    assert settlement_day_close_utc(MIA, date(2026, 8, 18)) == "2026-08-19T05:00:00+00:00"

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            # 08:00Z on the 19th is exactly the close: already the next climate
            # day, and the bin's outcome is known.
            _insert_decision(
                conn, created_at="2026-08-19T08:00:00+00:00",
                target_date="2026-08-18", ticker="KXHIGHTSFO-26AUG18-B66.5",
                floor=66.0, cap=67.0,
            )
        summary = store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        assert summary["offered_bins"] == 0


def test_one_pass_per_lead_serves_every_city_on_its_own_clock():
    """The per-city loop rescanned the same target-date partition fifteen times
    (7.6 s against 0.7 s on production, on the box the audit flags for exhausted
    CPU credits). One pass carries each city's own fixed-standard boundary in a
    CASE instead -- and must still give exactly the per-city answer."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            # 06:30Z on the target date: already same-day in Miami (opens
            # 05:00Z), still day-ahead at SFO (opens 08:00Z). A single shared
            # cutoff would misclassify one of them.
            for ticker in ("KXHIGHTSFO-26AUG18-B66.5", "KXHIGHNY-26AUG18-B80.5"):
                _insert_decision(
                    conn, created_at="2026-08-17T18:00:00+00:00",
                    target_date="2026-08-18", ticker=ticker, floor=66.0, cap=67.0,
                )
            _insert_decision(
                conn, created_at="2026-08-18T06:30:00+00:00",
                target_date="2026-08-18", ticker="KXHIGHTSFO-26AUG18-B66.5",
                floor=66.0, cap=67.0, market_probability=0.91,
            )
            _insert_decision(
                conn, created_at="2026-08-18T06:30:00+00:00",
                target_date="2026-08-18", ticker="KXHIGHMIA-26AUG18-B90.5",
                floor=90.0, cap=91.0, market_probability=0.92,
            )
            quotes = {
                (quote.market_ticker, quote.side): quote
                for quote in offered_ladder_quotes(conn, target_date="2026-08-18")
            }

    sfo = quotes[("KXHIGHTSFO-26AUG18-B66.5", "NO")]
    assert sfo.quote_lead == QUOTE_LEAD_DAY_AHEAD
    assert sfo.quote_created_at == "2026-08-18T06:30:00+00:00"
    assert sfo.station_id == "KSFO"
    mia = quotes[("KXHIGHMIA-26AUG18-B90.5", "NO")]
    assert mia.quote_lead == QUOTE_LEAD_SAME_DAY
    assert mia.station_id == "KMIA"
    # A third city with only a day-ahead quote is unaffected.
    assert quotes[("KXHIGHNY-26AUG18-B80.5", "NO")].quote_lead == QUOTE_LEAD_DAY_AHEAD


def test_an_unknown_series_is_not_scored():
    """The CASE has no ELSE, so a retired or foreign ticker matches no city and
    contributes no bound -- which must mean "excluded", not "unbounded"."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _insert_decision(
                conn, created_at="2026-08-17T18:00:00+00:00",
                target_date="2026-08-18", ticker="KXNOTACITY-26AUG18-B66.5",
                floor=66.0, cap=67.0,
            )
            assert offered_ladder_quotes(conn, target_date="2026-08-18") == []


# ---------------------------------------------------------------------------
# Reporting -- a number that cannot be read wrong
# ---------------------------------------------------------------------------


def test_scoring_counts_every_row_it_drops():
    """`0 flagged` must never be readable as `everything agreed`, and a metric
    over silently fewer bins than the caller asked for is the whole failure mode
    this module exists to remove."""

    rows = [
        {
            "integrity_status": INTEGRITY_UNCHECKED, "side_won": 1, "traded": 0,
            "market_ticker": "A", "target_date": "2026-08-18",
            "series_ticker": "KXHIGHTSFO", "quote_lead": QUOTE_LEAD_DAY_AHEAD,
            "quote_risk_profile": "research",
            "model_probability": 0.5, "market_probability": 1.0,
        },
        {
            "integrity_status": INTEGRITY_OK, "side_won": 0, "traded": 0,
            "market_ticker": "B", "target_date": "2026-08-18",
            "series_ticker": "KXHIGHTSFO", "quote_lead": QUOTE_LEAD_SAME_DAY,
            "quote_risk_profile": "live",
            "model_probability": None, "market_probability": 0.0,
        },
        {
            "integrity_status": INTEGRITY_FLAGGED, "side_won": 0, "traded": 0,
            "market_ticker": "C", "target_date": "2026-08-18",
            "series_ticker": "KXHIGHTSFO", "quote_lead": QUOTE_LEAD_DAY_AHEAD,
            "quote_risk_profile": "research",
            "model_probability": 0.5, "market_probability": 0.5,
        },
    ]
    score = score_ladder_outcomes(rows)
    assert score["scored_bins"] == 1
    assert score["unchecked_bins"] == 1
    assert score["unscorable_bins"] == 1
    assert score["flagged_bins_excluded"] == 1
    # quote_lead is ~98% confounded with risk_profile in production, so a
    # profile split read as book skill is a wrong answer. Both mixes ship with
    # the metric.
    assert score["quote_leads"] == {QUOTE_LEAD_DAY_AHEAD: 1}
    assert score["risk_profiles"] == {"research": 1}


def test_scoring_refuses_to_score_a_truncated_read():
    """The read is ``ORDER BY target_date, market_ticker, side LIMIT ?``, so an
    overflowing range would otherwise return a Brier over the OLDEST prefix with
    nothing saying so. The ledger is designed to accumulate."""

    with TemporaryDirectory() as tmp:
        store = _store(tmp)
        with store.connect() as conn:
            _seed_one_sfo_day(conn)
        store.backfill_ladder_bin_outcomes(
            start="2026-08-18", end="2026-08-18",
            cli_settlement_highs={("KXHIGHTSFO", "2026-08-18"): 67.0},
        )
        assert store.score_ladder_bin_outcomes(limit=10)["scored_bins"] == 3
        with pytest.raises(ValueError, match="truncated prefix"):
            store.score_ladder_bin_outcomes(limit=3)


# ---------------------------------------------------------------------------
# The operator command -- exit status is the nightly unit's only alert channel
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Stands in for SfoForecasterAdapter, which owns weather.db."""

    cli_highs: dict = {}
    observed_highs: dict = {}
    observed_min: list = []

    def __init__(self, _root):
        pass

    def load_cli_settlement_truth(self):
        return dict(_StubAdapter.cli_highs)

    def load_observed_daily_highs(self, *, min_observations):
        _StubAdapter.observed_min.append(min_observations)
        return dict(_StubAdapter.observed_highs)


def _ladder_args(tmp: str, *argv: str):
    from sfo_kalshi_quant._cli.parser import build_parser

    return build_parser().parse_args(
        [
            "--no-color",
            "--db-path", str(Path(tmp) / "paper.db"),
            "--forecaster-root", tmp,
            "paper-ladder-outcomes",
            *argv,
        ]
    )


def _run_ladder_cli(monkeypatch, tmp: str, *argv: str) -> int:
    from sfo_kalshi_quant._cli import paper as paper_cli

    monkeypatch.setattr(paper_cli, "SfoForecasterAdapter", _StubAdapter)
    return paper_cli.cmd_paper_ladder_outcomes(_ladder_args(tmp, *argv))


def test_the_score_defaults_to_the_harder_quote_class(tmp_path):
    """The module docstring says same-day quotes must not be pooled with
    day-ahead ones, and in production quote_lead is ~98% confounded with
    risk_profile -- the research book supplies almost every day-ahead quote and
    the live book almost every same-day one. A default that pools them invites
    a lead difference to be read as book skill."""

    args = _ladder_args(str(tmp_path), "--nightly")
    assert args.quote_lead == QUOTE_LEAD_DAY_AHEAD
    assert args.side == "NO"
    assert args.nightly_lookback_days == 3
    assert args.allow_incomplete is False


def test_the_nightly_pass_re_resolves_the_last_few_days(monkeypatch, tmp_path):
    """A station's final CLI for D-1 is issued 01:30-04:40 local, so a one-shot
    nightly leaves a permanent hole whenever it runs first: the bins come back
    in missing_truth, are written nowhere, and the next night resolves the next
    day. Re-resolving closes the hole."""

    import sfo_kalshi_quant._cli.paper as paper_cli

    captured = {}

    def _capture(self, **kwargs):
        captured.update(kwargs)
        return {
            "start": kwargs["start"], "end": kwargs["end"], "target_dates": 4,
            "offered_bins": 0, "resolved_bins": 0, "recorded_bins": 0,
            "missing_truth_bins": 0, "unchecked_bins": 0, "flagged": [],
            "missing_truth": [], "dry_run": False,
            "coverage": {
                "city_days": 0, "expected_bins_per_city_day": 0,
                "thin_city_days": [], "retention_full_fidelity_since": "2026-08-31",
                "retention_incomplete_dates": [],
            },
        }

    monkeypatch.setattr(PaperStore, "backfill_ladder_bin_outcomes", _capture)
    monkeypatch.setattr(
        paper_cli, "previous_complete_settlement_day", lambda: "2026-09-05"
    )
    assert _run_ladder_cli(monkeypatch, str(tmp_path), "--nightly") == 0
    assert captured["start"] == "2026-09-02"
    assert captured["end"] == "2026-09-05"


def test_the_command_exits_non_zero_on_an_integrity_contradiction(
    monkeypatch, tmp_path
):
    """``OnFailure=sfo-alert@%n.service`` is the unit's only production
    notification channel, so a run that found a bad label must not exit 0."""

    _StubAdapter.cli_highs = {("KXHIGHTSFO", "2026-08-18"): 67.0}
    _StubAdapter.observed_highs = {("KXHIGHTSFO", "2026-08-18"): 74.0}
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        _seed_one_sfo_day(conn)
    code = _run_ladder_cli(
        monkeypatch, str(tmp_path), "--start", "2026-08-18", "--end", "2026-08-18"
    )
    assert code == 1
    rows = store.ladder_bin_outcomes()
    assert {str(row["integrity_status"]) for row in rows} == {INTEGRITY_FLAGGED}
    # And the guard's density threshold reached the loader rather than being
    # re-invented at the call site.
    assert _StubAdapter.observed_min[-1] == LADDER_OBSERVED_MIN_OBSERVATIONS


def test_a_clean_recent_run_exits_zero_and_a_stale_hole_does_not(
    monkeypatch, tmp_path
):
    """A hole on the newest day in range is normal -- its CLI may still be hours
    away. A hole on an older day is one the lookback failed to close, and
    nothing else will ever revisit it."""

    import sfo_kalshi_quant.db as db_module

    # Pin the clock so the retention horizon is a fixed, in-range date rather
    # than "whenever this test runs".
    monkeypatch.setattr(db_module, "_now", lambda: "2026-08-20T00:00:00+00:00")
    _StubAdapter.observed_highs = {}
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        _seed_one_sfo_day(conn)
        for ticker, strike_type, floor, cap in (
            ("KXHIGHTSFO-26AUG19-B66.5", "between", 66.0, 67.0),
            ("KXHIGHTSFO-26AUG19-T64", "less", None, 64.0),
            ("KXHIGHTSFO-26AUG19-T70", "greater", 70.0, None),
        ):
            _insert_decision(
                conn, created_at="2026-08-18T18:00:00+00:00",
                target_date="2026-08-19", ticker=ticker, floor=floor, cap=cap,
                strike_type=strike_type,
            )

    _StubAdapter.cli_highs = {
        ("KXHIGHTSFO", "2026-08-18"): 67.0,
        ("KXHIGHTSFO", "2026-08-19"): 67.0,
    }
    argv = ("--start", "2026-08-18", "--end", "2026-08-19")
    assert _run_ladder_cli(monkeypatch, str(tmp_path), *argv) == 0

    # Now the OLDER day has no final CLI. That is a permanent hole, not a wait.
    _StubAdapter.cli_highs = {("KXHIGHTSFO", "2026-08-19"): 67.0}
    assert _run_ladder_cli(monkeypatch, str(tmp_path), *argv) == 1
    # It is a completeness alert, so a deliberate backfill can waive it.
    assert _run_ladder_cli(
        monkeypatch, str(tmp_path), *argv, "--allow-incomplete"
    ) == 0


def test_allow_incomplete_never_waives_a_bad_label(monkeypatch, tmp_path):
    """A thin ladder is the expected answer for a historical backfill. An
    integrity contradiction says a LABEL may be wrong, and no flag waives it."""

    _StubAdapter.cli_highs = {("KXHIGHTSFO", "2026-08-18"): 67.0}
    _StubAdapter.observed_highs = {("KXHIGHTSFO", "2026-08-18"): 74.0}
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        _seed_one_sfo_day(conn)
    assert _run_ladder_cli(
        monkeypatch, str(tmp_path),
        "--start", "2026-08-18", "--end", "2026-08-18", "--allow-incomplete",
    ) == 1


def test_allow_incomplete_waives_a_ladder_retention_has_thinned(
    monkeypatch, tmp_path
):
    """A deliberate historical backfill legitimately reads a ladder retention
    has already thinned; the nightly does not."""

    _StubAdapter.cli_highs = {
        ("KXHIGHTSFO", "2026-08-18"): 67.0,
        ("KXHIGHNY", "2026-08-18"): 81.0,
    }
    _StubAdapter.observed_highs = {}
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        _seed_one_sfo_day(conn)
        # A second city offering only one of the three bins: thin.
        _insert_decision(
            conn, created_at="2026-08-17T18:00:00+00:00", target_date="2026-08-18",
            ticker="KXHIGHNY-26AUG18-B80.5", floor=80.0, cap=81.0,
        )
    argv = ("--start", "2026-08-18", "--end", "2026-08-18")
    assert _run_ladder_cli(monkeypatch, str(tmp_path), *argv) == 1
    assert _run_ladder_cli(
        monkeypatch, str(tmp_path), *argv, "--allow-incomplete"
    ) == 0
