"""Task 4: replay research candidates through the exact exec-v3/v4 engine.

Covers point-in-time execution parity (plan Task 4 Step 1): no pre-decision
quote/trade use, exec-v3/v4 parity for fee/fill/queue math, fail-closed
behavior on incomplete market history, and that only the market/settlement
facts a case itself carries -- never another case's, never post-decision
book state -- ever enter its replay.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta, timezone

from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.execution import initial_queue_ahead, target_research_quote
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.probability import interval_probability_normal
from sfo_kalshi_quant.research_candidates import (
    GAUSSIAN_PIT_CANDIDATE_KEY,
    GAUSSIAN_PIT_CANDIDATE_VERSION,
    GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
    GOOGLE_RUNTIME_CANDIDATE_KEY,
    IDENTITY_CANDIDATE_KEY,
    IDENTITY_CANDIDATE_VERSION,
    IDENTITY_HYPOTHESIS_FAMILY,
    CandidateDistribution,
    identity_candidate,
)
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from sfo_kalshi_quant.research_replay import (
    case_replay_payload,
    replay_case_candidate,
    replay_fold_candidates,
)
from sfo_kalshi_quant.research_walkforward import ResearchCase, WalkForwardFold

_case_hash_counter = itertools.count()

# Distinguishes "market_snapshot not passed" (use the default fixture) from
# an explicit ``market_snapshot=None`` (deliberately no snapshot at all).
_DEFAULT_SNAPSHOT = object()


def _market_snapshot(
    *,
    ticker: str = "KXHIGHTSFO-TEST-GREATER",
    yes_bid: float = 0.20,
    yes_ask: float = 0.21,
    yes_bid_size: float = 50.0,
    yes_ask_size: float = 10.0,
    floor_strike: float | None = 60.0,
    cap_strike: float | None = None,
    strike_type: str = "greater",
) -> dict[str, dict[str, object]]:
    entry: dict[str, object] = {
        "ticker": ticker,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_bid_size": yes_bid_size,
        "yes_ask_size": yes_ask_size,
        "strike_type": strike_type,
    }
    if floor_strike is not None:
        entry["floor_strike"] = floor_strike
    if cap_strike is not None:
        entry["cap_strike"] = cap_strike
    return {ticker: entry}


def _case(
    *,
    station_id: str = "KSFO",
    target_date: date = date(2026, 6, 20),
    lead_days: int = 1,
    actual_high_f: float = 75.0,
    source_context_hash: str | None = None,
    market_snapshot: dict[str, dict[str, object]] | None | object = _DEFAULT_SNAPSHOT,
) -> ResearchCase:
    decision_at = datetime(
        target_date.year, target_date.month, target_date.day, 20, 0, 0, tzinfo=timezone.utc
    ) - timedelta(days=lead_days)
    settled_at = datetime(
        target_date.year, target_date.month, target_date.day, 20, 0, 0, tzinfo=timezone.utc
    ) + timedelta(days=1, hours=8)
    resolved_snapshot = (
        _market_snapshot() if market_snapshot is _DEFAULT_SNAPSHOT else market_snapshot
    )
    return ResearchCase(
        station_id=station_id,
        target_date=target_date,
        decision_at=decision_at,
        settled_at=settled_at,
        lead_days=lead_days,
        source_context_hash=source_context_hash
        or f"{station_id}:{target_date.isoformat()}:{lead_days}:{next(_case_hash_counter)}",
        baseline_mu=70.0,
        baseline_sigma=3.0,
        actual_high_f=actual_high_f,
        market_snapshot=resolved_snapshot,
    )


def _identity(mu: float = 70.0, sigma: float = 3.0) -> CandidateDistribution:
    return CandidateDistribution(
        candidate_key=IDENTITY_CANDIDATE_KEY,
        candidate_version=IDENTITY_CANDIDATE_VERSION,
        hypothesis_family=IDENTITY_HYPOTHESIS_FAMILY,
        available=True,
        mu=mu,
        sigma=sigma,
    )


def _unavailable_challenger(reason: str = "no_pooled_training_history") -> CandidateDistribution:
    return CandidateDistribution(
        candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY,
        candidate_version=GAUSSIAN_PIT_CANDIDATE_VERSION,
        hypothesis_family=GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
        available=False,
        unavailable_reason=reason,
    )


def _fold(train: tuple[ResearchCase, ...], test: tuple[ResearchCase, ...]) -> WalkForwardFold:
    return WalkForwardFold(
        fold_id=f"{test[0].station_id}:{test[0].target_date.isoformat()}",
        decision_at=min(c.decision_at for c in test),
        train=train,
        test=test,
    )


# ---------------------------------------------------------------------------
# Fail-closed: never a fabricated fill.
# ---------------------------------------------------------------------------


def test_missing_market_snapshot_is_a_recorded_skip_not_a_fabricated_fill() -> None:
    case = _case(market_snapshot=None)
    evidence = replay_case_candidate(case, _identity())

    assert evidence.available is False
    assert evidence.skip_reason == "missing_market_snapshot"
    assert evidence.tickers == ()
    assert evidence.realized_pnl == 0.0


def test_unavailable_candidate_distribution_is_a_recorded_skip() -> None:
    case = _case()
    evidence = replay_case_candidate(case, _unavailable_challenger("no_pooled_training_history"))

    assert evidence.available is False
    assert "no_pooled_training_history" in evidence.skip_reason
    assert evidence.tickers == ()


def test_market_entry_missing_bracket_bounds_is_invalid_market_entry() -> None:
    snapshot = _market_snapshot(strike_type="", floor_strike=None, cap_strike=None)
    case = _case(market_snapshot=snapshot)
    evidence = replay_case_candidate(case, _identity())

    assert evidence.available is True
    assert len(evidence.tickers) == 1
    assert evidence.tickers[0].status == "invalid_market_entry"
    assert evidence.realized_pnl == 0.0


def test_market_entry_with_out_of_range_ask_is_invalid_market_entry() -> None:
    # Genuinely out-of-[0,1] (not merely *at* a bound -- see the taxonomy
    # split covered by test_boundary_yes_ask_on_well_formed_bracket_is_no_
    # trade_not_invalid below) is real corruption: no order book can quote
    # an ask above $1.
    snapshot = _market_snapshot(yes_ask=1.5)
    case = _case(market_snapshot=snapshot)
    evidence = replay_case_candidate(case, _identity())

    assert evidence.tickers[0].status == "invalid_market_entry"


def test_market_entry_ticker_mismatch_is_invalid_market_entry() -> None:
    snapshot = _market_snapshot(ticker="KXHIGHTSFO-A")
    snapshot["KXHIGHTSFO-A"]["ticker"] = "KXHIGHTSFO-B"
    case = _case(market_snapshot=snapshot)
    evidence = replay_case_candidate(case, _identity())

    assert evidence.tickers[0].status == "invalid_market_entry"


# ---------------------------------------------------------------------------
# Crossing fills replay through the real fee/fill engine.
# ---------------------------------------------------------------------------


def test_strong_edge_crosses_and_fills_with_realistic_pnl() -> None:
    # floor=60, "greater" -> resolves YES iff actual_high_f > 60. mu=70 puts
    # the fitted distribution's mass overwhelmingly above 60.5, so a cheap
    # 0.21 ask is a large, unambiguous edge that must cross (spread == tick).
    case = _case(actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))

    assert evidence.available is True
    outcome = evidence.tickers[0]
    assert outcome.status == "filled"
    assert outcome.would_cross is True
    assert outcome.contracts is not None and outcome.contracts >= 1.0
    assert outcome.limit_price == 0.21
    # Settlement resolves YES (75 > 60): realized P&L is contracts * (1 -
    # cost_per_contract), strictly positive on a cheap, correctly-called bet.
    assert outcome.realized_pnl > 0.0
    assert evidence.realized_pnl == outcome.realized_pnl


def test_fee_matches_the_real_quadratic_fee_schedule_exactly() -> None:
    case = _case(actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))
    outcome = evidence.tickers[0]
    assert outcome.status == "filled"

    config = StrategyConfig()
    expected_fee = quadratic_fee_average_per_contract(
        outcome.limit_price,
        outcome.contracts,
        maker=not outcome.would_cross,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker="KXHIGHTSFO-TEST-GREATER",
    )
    assert outcome.fee_per_contract == expected_fee


def test_probability_matches_interval_probability_normal_over_the_bracket_bound() -> None:
    case = _case(actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))
    outcome = evidence.tickers[0]

    market_bin = MarketBin(
        ticker="KXHIGHTSFO-TEST-GREATER", event_ticker="", title="", yes_sub_title="",
        strike_type="greater", floor_strike=60.0, cap_strike=None,
        yes_bid=0.20, yes_ask=0.21, no_bid=0.79, no_ask=0.80,
        yes_bid_size=50.0, yes_ask_size=10.0, status="",
    )
    lo, hi = market_bin.continuous_interval()
    expected_probability = interval_probability_normal(70.0, 3.0, lo, hi)
    assert outcome.probability == expected_probability


def test_realized_pnl_matches_a_direct_run_replay_call() -> None:
    """Parity: the module's realized P&L for a filled order must equal what
    calling target_research_quote + run_replay directly, by hand, produces
    for the equivalent decision -- pinning to the real engine, not a second
    formula."""

    case = _case(actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))
    outcome = evidence.tickers[0]
    assert outcome.status == "filled"

    from sfo_kalshi_quant.replay import ReplayOrder, SettlementFact, build_exec_v3_events, run_replay

    market_bin = MarketBin(
        ticker="KXHIGHTSFO-TEST-GREATER", event_ticker="", title="", yes_sub_title="",
        strike_type="greater", floor_strike=60.0, cap_strike=None,
        yes_bid=0.20, yes_ask=0.21, no_bid=0.79, no_ask=0.80,
        yes_bid_size=50.0, yes_ask_size=10.0, status="",
    )
    lo, hi = market_bin.continuous_interval()
    probability = interval_probability_normal(70.0, 3.0, lo, hi)
    budget = TARGET_POLICY.reference_equity * TARGET_POLICY.max_position_risk_pct
    contracts = float(int(budget // market_bin.yes_ask))
    decision = TradeDecision(
        ticker=market_bin.ticker, label=market_bin.ticker, action="research_replay",
        approved=True, probability=probability, probability_lcb=probability,
        yes_bid=market_bin.yes_bid, yes_ask=market_bin.yes_ask, spread=market_bin.spread,
        fee_per_contract=0.0, cost_per_contract=0.0, edge=0.0, edge_lcb=0.0,
        kelly_fraction=0.0, recommended_contracts=contracts, expected_profit=0.0,
        reasons=[], side="YES", entry_bid=market_bin.yes_bid, entry_ask=market_bin.yes_ask,
        entry_bid_size=market_bin.yes_bid_size, entry_ask_size=market_bin.yes_ask_size,
        strike_type=market_bin.strike_type, floor_strike=market_bin.floor_strike,
        cap_strike=market_bin.cap_strike,
    )
    quote = target_research_quote(decision, StrategyConfig())
    assert quote is not None
    order = ReplayOrder(
        order_id="parity", placed_at=case.decision_at, target_date=case.target_date.isoformat(),
        ticker=market_bin.ticker, side="YES", limit_price=quote.price, contracts=quote.contracts,
        fee_per_contract=quote.fee_per_contract, immediate=quote.would_cross,
    )
    settlement = SettlementFact(
        ticker=market_bin.ticker, target_date=case.target_date.isoformat(),
        settled_at=case.settled_at, resolved_yes=True,
    )
    expected = run_replay(
        list(build_exec_v3_events(orders=[order], settlements=[settlement])),
        initial_capital=TARGET_POLICY.reference_equity,
    )

    assert outcome.realized_pnl == expected.realized_pnl
    assert outcome.contracts == quote.contracts
    assert outcome.limit_price == quote.price


# ---------------------------------------------------------------------------
# No post-decision book state: a resting order never fabricates a fill.
# ---------------------------------------------------------------------------


def test_resting_maker_order_never_fabricates_a_fill() -> None:
    # Wide spread (bid=0.10, ask=0.90): one tick of improvement over the bid
    # (0.11) does not reach the ask, so target_research_quote prices this as
    # a resting, inside-spread maker order, never a crossing taker fill.
    snapshot = _market_snapshot(yes_bid=0.10, yes_ask=0.90, yes_ask_size=100.0)
    case = _case(market_snapshot=snapshot, actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))

    outcome = evidence.tickers[0]
    assert outcome.status in ("unfilled_expired", "no_trade")
    if outcome.status == "unfilled_expired":
        assert outcome.would_cross is False
        assert outcome.realized_pnl == 0.0
    assert evidence.realized_pnl == 0.0


def test_queue_ahead_matches_the_real_initial_queue_ahead_formula() -> None:
    snapshot = _market_snapshot(yes_bid=0.10, yes_ask=0.90, yes_bid_size=42.0, yes_ask_size=100.0)
    case = _case(market_snapshot=snapshot, actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))
    outcome = evidence.tickers[0]
    assert outcome.status == "unfilled_expired"
    assert outcome.would_cross is False

    # The resting order posts one tick above the visible bid (0.11), a new
    # best price with no displayed queue known ahead of it -- calling the
    # real formula directly with the outcome's own limit price must match
    # exactly what the module used internally to build the ReplayOrder.
    assert outcome.queue_ahead == initial_queue_ahead(
        outcome.limit_price, 0.10, 42.0
    )
    assert outcome.queue_ahead == 0.0


def test_negative_edge_produces_no_trade_not_a_fabricated_skip() -> None:
    # Bracket the fitted distribution almost never reaches, priced
    # expensively: after-fee edge is negative, so no trade is the correct,
    # non-fabricated outcome (not a data-quality failure).
    snapshot = _market_snapshot(
        ticker="KXHIGHTSFO-TEST-COLD", floor_strike=None, cap_strike=40.0,
        strike_type="less", yes_bid=0.94, yes_ask=0.95, yes_ask_size=10.0,
    )
    case = _case(market_snapshot=snapshot, actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))

    outcome = evidence.tickers[0]
    assert outcome.status == "no_trade"
    assert outcome.realized_pnl == 0.0


# ---------------------------------------------------------------------------
# Determinism and chronological purity.
# ---------------------------------------------------------------------------


def test_replay_case_candidate_is_deterministic() -> None:
    case = _case(actual_high_f=75.0)
    candidate = _identity(mu=70.0, sigma=3.0)

    first = replay_case_candidate(case, candidate)
    second = replay_case_candidate(case, candidate)

    assert first == second


def test_replay_case_candidate_depends_only_on_its_own_case_and_candidate_fields() -> None:
    """Two structurally different cases (different station/hash/lead) that
    happen to share the same decision-relevant fields must replay
    identically -- proving nothing about a case's identity, or any other
    case, leaks into its own money outcome."""

    snapshot = _market_snapshot()
    common = dict(
        target_date=date(2026, 6, 20),
        actual_high_f=75.0,
        market_snapshot=snapshot,
    )
    case_a = _case(station_id="KSFO", lead_days=1, source_context_hash="hash-a", **common)
    case_b = _case(station_id="KLAX", lead_days=2, source_context_hash="hash-b", **common)
    candidate = _identity(mu=70.0, sigma=3.0)

    evidence_a = replay_case_candidate(case_a, candidate)
    evidence_b = replay_case_candidate(case_b, candidate)

    assert [t.status for t in evidence_a.tickers] == [t.status for t in evidence_b.tickers]
    assert evidence_a.realized_pnl == evidence_b.realized_pnl


def test_market_derived_price_is_identical_across_candidates_when_both_fill() -> None:
    """The limit price a filled order clears at comes entirely from the
    market snapshot (bid/ask/tick), never from a candidate's own mu/sigma --
    proving the same underlying market facts are used no matter which
    candidate is being replayed (plan Task 4 Step 1: "identical event
    streams for paired baseline/challenger replay")."""

    case = _case(actual_high_f=75.0)
    strong = replay_case_candidate(case, _identity(mu=70.0, sigma=1.0))
    weaker = replay_case_candidate(case, _identity(mu=64.0, sigma=1.0))

    strong_outcome = strong.tickers[0]
    weaker_outcome = weaker.tickers[0]
    assert strong_outcome.status == "filled"
    assert weaker_outcome.status == "filled"
    assert strong_outcome.limit_price == weaker_outcome.limit_price
    assert strong_outcome.would_cross == weaker_outcome.would_cross is True
    # Only the probability -- and hence edge -- differs between candidates.
    assert strong_outcome.probability != weaker_outcome.probability


# ---------------------------------------------------------------------------
# case_replay_payload / replay_fold_candidates shape.
# ---------------------------------------------------------------------------


def test_case_replay_payload_is_json_safe() -> None:
    import json

    case = _case(actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))
    payload = case_replay_payload(evidence)

    json.dumps(payload, allow_nan=False)
    assert payload["candidate_key"] == IDENTITY_CANDIDATE_KEY
    assert payload["available"] is True
    assert len(payload["tickers"]) == 1


def test_replay_fold_candidates_shapes_one_row_per_challenger_keyed_by_case_hash() -> None:
    train = (
        _case(
            station_id="KSFO", target_date=date(2026, 6, 10), lead_days=1,
            actual_high_f=68.0, source_context_hash="train-1",
        ),
    )
    test_case = _case(
        station_id="KSFO", target_date=date(2026, 6, 20), lead_days=1,
        actual_high_f=75.0, source_context_hash="test-1",
    )
    fold = _fold(train, (test_case,))

    evidence_rows = replay_fold_candidates(fold)

    assert len(evidence_rows) == 2
    keys = {row.challenger_candidate_key for row in evidence_rows}
    assert keys == {GAUSSIAN_PIT_CANDIDATE_KEY, GOOGLE_RUNTIME_CANDIDATE_KEY}
    for row in evidence_rows:
        assert set(row.baseline["cases"].keys()) == {"test-1"}
        assert set(row.challenger["cases"].keys()) == {"test-1"}
        assert row.baseline["cases"]["test-1"]["candidate_key"] == IDENTITY_CANDIDATE_KEY
        assert row.fold_id == fold.fold_id
        assert row.station_id == "KSFO"
        assert row.target_date == date(2026, 6, 20)


def test_replay_fold_candidates_blocks_promotion_on_missing_market_snapshot() -> None:
    train = (
        _case(
            station_id="KSFO", target_date=date(2026, 6, 10), lead_days=1,
            actual_high_f=68.0, source_context_hash="train-1",
        ),
    )
    test_case = _case(
        station_id="KSFO", target_date=date(2026, 6, 20), lead_days=1,
        actual_high_f=75.0, source_context_hash="test-1", market_snapshot=None,
    )
    fold = _fold(train, (test_case,))

    evidence_rows = replay_fold_candidates(fold)

    for row in evidence_rows:
        assert row.promotion_eligible is False
        assert any("missing_market_snapshot" in reason for reason in row.promotion_block_reasons)


def test_replay_fold_candidates_is_promotion_eligible_when_every_case_replays_cleanly() -> None:
    train = (
        _case(
            station_id="KSFO", target_date=date(2026, 6, 10), lead_days=1,
            actual_high_f=68.0, source_context_hash="train-1",
        ),
    )
    test_case = _case(
        station_id="KSFO", target_date=date(2026, 6, 20), lead_days=1,
        actual_high_f=75.0, source_context_hash="test-1",
    )
    fold = _fold(train, (test_case,))

    evidence_rows = replay_fold_candidates(fold)
    by_key = {row.challenger_candidate_key: row for row in evidence_rows}

    # gaussian-pit-station-lead-v1 has a matching station/lead training
    # cohort (the anchor row above) and this fixture's market snapshot
    # replays cleanly -- fully promotion eligible.
    gaussian_row = by_key[GAUSSIAN_PIT_CANDIDATE_KEY]
    assert gaussian_row.promotion_eligible is True
    assert gaussian_row.promotion_block_reasons == ()

    # google-runtime-fixed-v1 is correctly blocked here for an unrelated,
    # accurate reason: this fixture attaches no Google challenger evidence
    # to the case at all (Google Task 7's durable evidence is out of this
    # task's scope) -- not a Task 4 replay defect.
    google_row = by_key[GOOGLE_RUNTIME_CANDIDATE_KEY]
    assert google_row.promotion_eligible is False
    assert any(
        "no_google_evidence_for_case" in reason
        for reason in google_row.promotion_block_reasons
    )


# ---------------------------------------------------------------------------
# F1: per-ticker invalid_market_entry outcomes must block promotion.
# ---------------------------------------------------------------------------


def test_market_entry_with_missing_ask_and_no_strikes_is_invalid_market_entry() -> None:
    # Reviewer probe construction: a bare ticker entry with only a bid --
    # no ask, no strikes -- can never be parsed into a tradeable, bounded
    # quote.
    snapshot = _market_snapshot()
    snapshot["KX-BAD"] = {"ticker": "KX-BAD", "yes_bid": 0.10}
    case = _case(market_snapshot=snapshot)
    evidence = replay_case_candidate(case, _identity())

    bad = next(t for t in evidence.tickers if t.ticker == "KX-BAD")
    assert bad.status == "invalid_market_entry"


def test_replay_fold_candidates_blocks_promotion_on_invalid_market_entry() -> None:
    """Reviewer probe: a fold whose test case carries one valid entry plus
    one malformed entry (a ticker with no ask and no strikes) must not be
    promotion eligible. Before the fix, per-ticker invalid_market_entry
    outcomes were silently dropped -- only whole-case unavailability ever
    produced a block reason -- so this fold wrongly reported
    promotion_eligible=True."""

    train = (
        _case(
            station_id="KSFO", target_date=date(2026, 6, 10), lead_days=1,
            actual_high_f=68.0, source_context_hash="train-1",
        ),
    )
    snapshot = _market_snapshot()
    snapshot["KX-BAD"] = {"ticker": "KX-BAD", "yes_bid": 0.10}
    test_case = _case(
        station_id="KSFO", target_date=date(2026, 6, 20), lead_days=1,
        actual_high_f=75.0, source_context_hash="test-1", market_snapshot=snapshot,
    )
    fold = _fold(train, (test_case,))

    evidence_rows = replay_fold_candidates(fold)

    for row in evidence_rows:
        assert row.promotion_eligible is False
        assert any(
            "test-1" in reason and "KX-BAD" in reason and "invalid_market_entry" in reason
            for reason in row.promotion_block_reasons
        )


# ---------------------------------------------------------------------------
# F1 taxonomy split: a well-formed, boundary-ask bracket is no_trade, not
# invalid_market_entry -- only genuine corruption blocks.
# ---------------------------------------------------------------------------


def test_boundary_yes_ask_on_well_formed_bracket_is_no_trade_not_invalid() -> None:
    # yes_ask sits exactly at the 1.0 bound -- an ordinary empty-ask,
    # one-sided book. Every other field is well-formed (matching ticker,
    # finite bid, real strikes). This is a legitimate no-trade outcome
    # (live target_research_quote/_reference_sized_decision would also
    # decline it), not corrupted data, and must not block promotion.
    snapshot = _market_snapshot(yes_bid=0.0, yes_ask=1.0)
    case = _case(market_snapshot=snapshot, actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))

    assert evidence.tickers[0].status == "no_trade"


def test_boundary_yes_ask_does_not_block_fold_promotion() -> None:
    train = (
        _case(
            station_id="KSFO", target_date=date(2026, 6, 10), lead_days=1,
            actual_high_f=68.0, source_context_hash="train-1",
        ),
    )
    snapshot = _market_snapshot(yes_bid=0.0, yes_ask=1.0)
    test_case = _case(
        station_id="KSFO", target_date=date(2026, 6, 20), lead_days=1,
        actual_high_f=75.0, source_context_hash="test-1", market_snapshot=snapshot,
    )
    fold = _fold(train, (test_case,))

    evidence_rows = replay_fold_candidates(fold)
    gaussian_row = next(
        row for row in evidence_rows if row.challenger_candidate_key == GAUSSIAN_PIT_CANDIDATE_KEY
    )
    assert gaussian_row.promotion_eligible is True
    assert not any("invalid_market_entry" in r for r in gaussian_row.promotion_block_reasons)


# ---------------------------------------------------------------------------
# F2: a crossed book is corrupted data, never a fabricated cheap fill.
# ---------------------------------------------------------------------------


def test_crossed_book_is_invalid_market_entry_not_a_cheap_fill() -> None:
    snapshot = _market_snapshot(yes_bid=0.50, yes_ask=0.40)
    case = _case(market_snapshot=snapshot, actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))

    assert evidence.tickers[0].status == "invalid_market_entry"
    assert evidence.tickers[0].realized_pnl == 0.0


def test_crossed_book_blocks_fold_promotion() -> None:
    train = (
        _case(
            station_id="KSFO", target_date=date(2026, 6, 10), lead_days=1,
            actual_high_f=68.0, source_context_hash="train-1",
        ),
    )
    snapshot = _market_snapshot(yes_bid=0.50, yes_ask=0.40)
    test_case = _case(
        station_id="KSFO", target_date=date(2026, 6, 20), lead_days=1,
        actual_high_f=75.0, source_context_hash="test-1", market_snapshot=snapshot,
    )
    fold = _fold(train, (test_case,))

    evidence_rows = replay_fold_candidates(fold)
    for row in evidence_rows:
        assert row.promotion_eligible is False
        assert any("invalid_market_entry" in r for r in row.promotion_block_reasons)


# ---------------------------------------------------------------------------
# F4: persisted payloads are self-describing (execution/sizing identity).
# ---------------------------------------------------------------------------


def test_case_replay_payload_stamps_execution_and_sizing_identity() -> None:
    from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION

    case = _case(actual_high_f=75.0)
    evidence = replay_case_candidate(case, _identity(mu=70.0, sigma=3.0))
    payload = case_replay_payload(evidence)

    stamp = payload["stamp"]
    assert stamp["execution_model_version"] == EXECUTION_MODEL_VERSION
    assert stamp["reference_equity"] == TARGET_POLICY.reference_equity
    assert stamp["max_position_risk_pct"] == TARGET_POLICY.max_position_risk_pct
    assert stamp["policy_fingerprint"] == TARGET_POLICY.policy_fingerprint
    assert stamp["order_ttl_minutes"] == 15
    assert stamp["side_scope"] == "yes_only"
    assert stamp["fill_scope"] == "taker_only_no_tape"

    import json

    json.dumps(payload, allow_nan=False)


def test_fold_replay_evidence_stamps_execution_and_sizing_identity() -> None:
    from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION

    train = (
        _case(
            station_id="KSFO", target_date=date(2026, 6, 10), lead_days=1,
            actual_high_f=68.0, source_context_hash="train-1",
        ),
    )
    test_case = _case(
        station_id="KSFO", target_date=date(2026, 6, 20), lead_days=1,
        actual_high_f=75.0, source_context_hash="test-1",
    )
    fold = _fold(train, (test_case,))

    evidence_rows = replay_fold_candidates(fold)

    for row in evidence_rows:
        for payload in (row.baseline, row.challenger):
            stamp = payload["stamp"]
            assert stamp["execution_model_version"] == EXECUTION_MODEL_VERSION
            assert stamp["policy_fingerprint"] == TARGET_POLICY.policy_fingerprint
            assert stamp["side_scope"] == "yes_only"
            assert stamp["fill_scope"] == "taker_only_no_tape"
            assert stamp["order_ttl_minutes"] == 15
