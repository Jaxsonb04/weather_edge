"""Task 4: replay research candidates through the exact exec-v3/v4 engine.

Bridges Task 3's fitted, per-case ``CandidateDistribution`` (mu, sigma) --
purely distributional, with no market or money attached -- into an
executable-realistic paired baseline/challenger P&L, replayed through the
*same* fill/fee/queue/crossing/expiry engine production paper trading uses
(``replay.run_replay``), against the *same* fee schedule
(``fees.quadratic_fee_average_per_contract``, called by
``execution.target_research_quote``) and the *same* bracket-probability and
settlement-resolution math the live decision pipeline uses
(``probability.interval_probability_normal``, ``models.MarketBin``).
Nothing here re-derives fill, fee, or settlement math from scratch -- every
money-affecting formula is a direct call into the existing, already-tested
production function.

Chronological integrity: a ``ResearchCase`` carries exactly one point-in-time
market snapshot -- the same scan-time ``market`` payload that already
contributes to its own ``source_context_hash`` (see
``research_walkforward.ResearchCase.market_snapshot``) -- and no ongoing
public trade tape. There is therefore no post-decision book state this
module could consume even by accident: a decision that does not cross the
visible ask at scan time (``execution.target_research_quote`` prices it as a
resting, inside-spread maker order) can never be proven to fill from this
evidence alone, and is replayed as unfilled-at-expiry rather than guessing
either way. This intentionally narrows "exact exec-v3 mechanics" to the
immediate/crossing-fill path for research replay specifically: queue-ahead
accounting (``execution.initial_queue_ahead``) is still computed exactly as
production does for a resting order, but with no trade tape to consume it,
the shared ``run_replay`` engine's own TTL-expiry path is what naturally,
non-fabricated-ly resolves it to zero filled contracts.

This module never talks to the database, the clock, or any random state:
every function here is a pure function of its own arguments.

RULING-A: the walk-forward plan (Task 4) names this replay's target engine
"exec-v3" as of plan-writing. The production engine has since been
re-versioned in place to ``exec-v4-2026-07-17``
(``maker_fills.EXECUTION_MODEL_VERSION``) by the maker-queue fix; this
module deliberately replays through that current, corrected engine rather
than pinning to the older exec-v3 identity the plan named (spec Sec 8.5,
Sec 9 P1-3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal, Mapping

from .config import StrategyConfig
from .execution import initial_queue_ahead, target_research_quote
from .maker_fills import EXECUTION_MODEL_VERSION
from .models import MarketBin, TradeDecision
from .probability import interval_probability_normal
from .replay import ReplayOrder, SettlementFact, build_exec_v3_events, run_replay
from .research_candidates import (
    DEFAULT_SHRINKAGE_K,
    GAUSSIAN_PIT_CANDIDATE_KEY,
    GOOGLE_RUNTIME_CANDIDATE_KEY,
    CandidateDistribution,
    fit_fold_candidates,
)
from .research_policy import TARGET_POLICY
from .research_walkforward import ResearchCase, WalkForwardFold
from .settlement_truth import integer_settlement_high_f

TickerReplayStatus = Literal["invalid_market_entry", "no_trade", "unfilled_expired", "filled"]

# Every order this module ever constructs prices/sizes through
# execution.target_research_quote against the YES side of the observed
# bracket only. Buying NO would require reproducing the live strategy's own
# side-selection heuristic (which side has better after-fee edge) -- a
# decision-policy concern, not an "exact exec mechanics and fees" one -- so
# it is out of scope here rather than risk a silently inverted P&L sign.
_SIDE = "YES"

# A resting (non-crossing) order rides on the live engine's standard 15
# minute TTL, matching every other ReplayOrder in this codebase
# (replay.py's own default and replay_from_database's construction).
_TTL_MINUTES = 15

# Scope labels stamped onto every persisted payload (F4): this module only
# ever prices/sizes the YES side (see ``_SIDE`` above) and only ever fills
# via an immediate/crossing taker match or a TTL expiry -- never a maker
# fill inferred from a later public trade tape, which this module has none
# of (see module docstring). Naming these explicitly keeps a downstream
# reader from assuming a persisted row covers more than it does.
_SIDE_SCOPE = "yes_only"
_FILL_SCOPE = "taker_only_no_tape"


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class TickerReplayOutcome:
    """One candidate's exec-replay outcome for one ticker in one test case.

    ``status`` distinguishes three fundamentally different "no P&L"
    outcomes that must never be conflated:

    - ``invalid_market_entry``: the snapshot entry for this ticker could not
      be parsed into a tradeable, bounded quote (fail closed -- never a
      fabricated fill).
    - ``no_trade``: the entry parsed fine, but ``target_research_quote``
      found no non-negative after-fee edge -- a legitimate "this candidate
      would not have traded" result, not a data problem.
    - ``unfilled_expired``: an order was constructed and replayed but never
      filled before its TTL (only reachable for a resting, non-crossing
      order -- see module docstring); zero realized P&L is the honest,
      non-fabricated outcome, not an assumption either way.
    - ``filled``: the order filled (always immediately/at-cross in this
      module -- see docstring) and its position was resolved against the
      case's own settlement.
    """

    ticker: str
    status: TickerReplayStatus
    detail: str = ""
    side: str | None = None
    would_cross: bool | None = None
    limit_price: float | None = None
    contracts: float | None = None
    fee_per_contract: float | None = None
    queue_ahead: float | None = None
    probability: float | None = None
    edge: float | None = None
    edge_lcb: float | None = None
    realized_pnl: float = 0.0


@dataclass(frozen=True)
class CaseReplayEvidence:
    """One candidate's exec-replay outcome for one test case, across every
    ticker observed in its own scan-time market snapshot."""

    source_context_hash: str
    candidate_key: str
    available: bool
    skip_reason: str = ""
    tickers: tuple[TickerReplayOutcome, ...] = ()

    @property
    def realized_pnl(self) -> float:
        return sum(ticker.realized_pnl for ticker in self.tickers)

    @property
    def filled_count(self) -> int:
        return sum(1 for ticker in self.tickers if ticker.status == "filled")


@dataclass(frozen=True)
class FoldReplayEvidence:
    """One challenger's fold-grained paired exec-replay bundle.

    Mirrors ``research_candidates.FoldCandidateEvidence``'s shape exactly
    (one row per ``(fold, challenger)``, cases keyed by
    ``source_context_hash`` inside ``{"cases": {...}}``) for the same
    structural reason: a fold's ``test`` tuple routinely holds more than one
    case sharing one station/target-day, and Task 1's ``research_evidence``
    primary key is fold-grained, not case-grained.
    """

    fold_id: str
    station_id: str
    target_date: date
    challenger_candidate_key: str
    baseline: dict[str, object]
    challenger: dict[str, object]
    promotion_eligible: bool
    promotion_block_reasons: tuple[str, ...] = ()


def _market_bin_from_snapshot_entry(
    ticker: str, entry: object
) -> MarketBin | None:
    """Parse one scan-time market snapshot entry into a ``MarketBin``.

    Fails closed (returns ``None``, which ``_replay_ticker`` reports as the
    blocking ``invalid_market_entry`` status) on anything that would let
    ``execution.target_research_quote`` or bracket-probability scoring
    silently misprice or mis-gate a decision: a missing/mismatched ticker, a
    non-finite or out-of-[0,1] quote, a crossed book (``yes_bid >=
    yes_ask`` -- never a real order book state, and never replayed as a
    fabricated cheap fill), or a bracket with no usable bound at all. Never
    guesses a missing field -- an omitted depth size becomes a known-zero
    displayed size (the same conservative default
    ``execution.initial_queue_ahead`` documents), not a skip, since a
    downstream zero-depth quote naturally fails ``target_research_quote``'s
    own liquidity checks rather than fabricating tradeable size.

    A *well-formed* bracket whose ``yes_ask`` sits exactly at a bound (0.0
    or 1.0 -- an ordinary empty-ask, one-sided book) is deliberately still
    parsed into a ``MarketBin`` here rather than rejected: this is
    legitimate, non-corrupted market data, and ``_reference_sized_decision``
    already carries the identical ``0.0 < yes_ask < 1.0`` gate that live
    ``target_research_quote`` sizing applies, which naturally resolves it to
    the non-blocking ``no_trade`` status downstream. Rejecting it here would
    conflate "no market" with "corrupted market" and wrongly block
    promotion on an everyday, honest no-trade outcome.
    """

    if not isinstance(entry, Mapping):
        return None
    entry_ticker = entry.get("ticker")
    if not isinstance(entry_ticker, str) or entry_ticker != ticker:
        return None
    yes_bid = _finite_float(entry.get("yes_bid"))
    yes_ask = _finite_float(entry.get("yes_ask"))
    if yes_bid is None or yes_ask is None or yes_bid < 0.0 or not (0.0 <= yes_ask <= 1.0):
        return None
    if yes_bid >= yes_ask:
        # Crossed (or locked) book: never a real, tradeable order book
        # state. Fail closed rather than let a downstream taker-fill path
        # replay it as an implausibly cheap, fabricated-price fill.
        return None
    strike_type = entry.get("strike_type")
    strike_type = str(strike_type) if isinstance(strike_type, str) else ""
    floor_strike = _finite_float(entry.get("floor_strike"))
    cap_strike = _finite_float(entry.get("cap_strike"))
    if strike_type == "less":
        if cap_strike is None:
            return None
    elif strike_type == "greater":
        if floor_strike is None:
            return None
    elif floor_strike is None or cap_strike is None:
        return None
    yes_bid_size = _finite_float(entry.get("yes_bid_size"))
    yes_ask_size = _finite_float(entry.get("yes_ask_size"))
    no_bid = _finite_float(entry.get("no_bid"))
    no_ask = _finite_float(entry.get("no_ask"))
    return MarketBin(
        ticker=ticker,
        event_ticker=str(entry.get("event_ticker") or ""),
        title=str(entry.get("title") or ""),
        yes_sub_title=str(entry.get("label") or ""),
        strike_type=strike_type,
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid if no_bid is not None else max(0.0, 1.0 - yes_ask),
        no_ask=no_ask if no_ask is not None else max(0.0, 1.0 - yes_bid),
        yes_bid_size=yes_bid_size or 0.0,
        yes_ask_size=yes_ask_size or 0.0,
        status=str(entry.get("status") or ""),
        result=str(entry.get("result") or ""),
        expiration_value=_finite_float(entry.get("expiration_value")),
    )


def _reference_sized_decision(
    market_bin: MarketBin,
    probability: float,
    *,
    reference_equity: float,
    max_position_risk_pct: float,
) -> TradeDecision | None:
    """Build the structural ``TradeDecision`` a candidate's fitted
    distribution implies for one bracket, sized to a fixed fraction of a
    fixed reference bankroll (``research_policy.TARGET_POLICY``'s own
    per-position risk cap over its $1,000 reference account -- reused, not
    invented) rather than a full Kelly optimization.

    Sizing here is a conservative, deterministic per-position budget cap,
    not a portfolio construction policy: this module does not enforce
    cross-ticker aggregate/city/region exposure caps
    (``research_portfolio.py``'s concern) across the several brackets one
    scan's market snapshot can carry. ``execution.target_research_quote``
    still applies the exact, unmodified after-fee edge gate and (when
    crossing) displayed-ask-depth cap on top of this starting budget, so a
    thin book or a negative edge still blocks or downsizes the trade exactly
    as it would live.

    ``probability_lcb`` is deliberately set equal to ``probability``: a
    ``ResearchCase`` carries a single fitted Gaussian per candidate, not a
    separate downside estimate, so there is no independent lower-confidence
    figure to plug in without inventing one. This makes the reused edge gate
    strictly MORE permissive than a decision with a genuine LCB would be --
    a real lower-confidence bound tightens the probability an edge is
    computed from, so skipping that tightening can only let through trades
    a live LCB gate would have blocked, never the reverse. Every candidate
    replayed through this module (baseline and challenger arms alike) gets
    this identical treatment, so the extra permissiveness is a shared,
    symmetric bias across both arms of the paired comparison, not a
    per-candidate advantage.
    """

    if not (0.0 < market_bin.yes_ask < 1.0):
        return None
    budget = max(0.0, reference_equity * max_position_risk_pct)
    contracts = math.floor(budget / market_bin.yes_ask)
    if contracts < 1.0:
        return None
    return TradeDecision(
        ticker=market_bin.ticker,
        label=market_bin.ticker,
        action="research_replay",
        approved=True,
        probability=probability,
        probability_lcb=probability,
        yes_bid=market_bin.yes_bid,
        yes_ask=market_bin.yes_ask,
        spread=market_bin.spread,
        fee_per_contract=0.0,
        cost_per_contract=0.0,
        edge=0.0,
        edge_lcb=0.0,
        kelly_fraction=0.0,
        recommended_contracts=float(contracts),
        expected_profit=0.0,
        reasons=[],
        side=_SIDE,
        entry_bid=market_bin.yes_bid,
        entry_ask=market_bin.yes_ask,
        entry_bid_size=market_bin.yes_bid_size,
        entry_ask_size=market_bin.yes_ask_size,
        strike_type=market_bin.strike_type or None,
        floor_strike=market_bin.floor_strike,
        cap_strike=market_bin.cap_strike,
    )


def _replay_ticker(
    ticker: str,
    entry: object,
    case: ResearchCase,
    mu: float,
    sigma: float,
    *,
    config: StrategyConfig,
    reference_equity: float,
    max_position_risk_pct: float,
) -> TickerReplayOutcome:
    market_bin = _market_bin_from_snapshot_entry(ticker, entry)
    if market_bin is None:
        return TickerReplayOutcome(
            ticker=ticker,
            status="invalid_market_entry",
            detail="market snapshot entry has no tradeable quote or bracket bound",
        )

    lo, hi = market_bin.continuous_interval()
    probability = interval_probability_normal(mu, sigma, lo, hi)

    decision = _reference_sized_decision(
        market_bin,
        probability,
        reference_equity=reference_equity,
        max_position_risk_pct=max_position_risk_pct,
    )
    if decision is None:
        return TickerReplayOutcome(
            ticker=ticker,
            status="no_trade",
            detail="reference sizing affords zero contracts at the visible ask",
            probability=probability,
        )

    quote = target_research_quote(decision, config)
    if quote is None:
        return TickerReplayOutcome(ticker=ticker, status="no_trade", probability=probability)

    queue_ahead = (
        0.0
        if quote.would_cross
        else initial_queue_ahead(quote.price, market_bin.yes_bid, market_bin.yes_bid_size)
    )
    order = ReplayOrder(
        order_id=f"{case.source_context_hash}:{ticker}",
        placed_at=case.decision_at,
        target_date=case.target_date.isoformat(),
        ticker=ticker,
        side=_SIDE,
        limit_price=quote.price,
        contracts=quote.contracts,
        fee_per_contract=quote.fee_per_contract,
        queue_ahead=queue_ahead,
        ttl_minutes=_TTL_MINUTES,
        immediate=quote.would_cross,
        queue_price=market_bin.yes_bid,
    )
    settlement = SettlementFact(
        ticker=ticker,
        target_date=case.target_date.isoformat(),
        settled_at=case.settled_at,
        resolved_yes=market_bin.resolves_yes(integer_settlement_high_f(case.actual_high_f)),
    )
    events = build_exec_v3_events(orders=[order], settlements=[settlement])
    result = run_replay(list(events), initial_capital=reference_equity)
    filled = result.filled >= 1
    return TickerReplayOutcome(
        ticker=ticker,
        status="filled" if filled else "unfilled_expired",
        side=_SIDE,
        would_cross=quote.would_cross,
        limit_price=quote.price,
        contracts=quote.contracts,
        fee_per_contract=quote.fee_per_contract,
        queue_ahead=queue_ahead,
        probability=probability,
        edge=quote.edge,
        edge_lcb=quote.edge_lcb,
        realized_pnl=result.realized_pnl if filled else 0.0,
    )


def replay_case_candidate(
    case: ResearchCase,
    candidate: CandidateDistribution,
    *,
    config: StrategyConfig = StrategyConfig(),
    reference_equity: float = TARGET_POLICY.reference_equity,
    max_position_risk_pct: float = TARGET_POLICY.max_position_risk_pct,
) -> CaseReplayEvidence:
    """Replay one candidate's implied decisions for one test case.

    A pure function of ``case`` and ``candidate`` alone -- it reads no other
    case, no fold, and no clock or random state, so mutating any other
    case's data (in the same fold or a different one) can never change this
    result (chronological-integrity guarantee, mirroring
    ``research_candidates``'s own case-purity guarantee for fitting/scoring).
    """

    if not candidate.available or candidate.mu is None or candidate.sigma is None:
        return CaseReplayEvidence(
            source_context_hash=case.source_context_hash,
            candidate_key=candidate.candidate_key,
            available=False,
            skip_reason=f"candidate_distribution_unavailable:{candidate.unavailable_reason}",
        )
    if not case.market_snapshot:
        return CaseReplayEvidence(
            source_context_hash=case.source_context_hash,
            candidate_key=candidate.candidate_key,
            available=False,
            skip_reason="missing_market_snapshot",
        )

    tickers = tuple(
        sorted(
            (
                _replay_ticker(
                    ticker,
                    entry,
                    case,
                    candidate.mu,
                    candidate.sigma,
                    config=config,
                    reference_equity=reference_equity,
                    max_position_risk_pct=max_position_risk_pct,
                )
                for ticker, entry in case.market_snapshot.items()
            ),
            key=lambda outcome: outcome.ticker,
        )
    )
    return CaseReplayEvidence(
        source_context_hash=case.source_context_hash,
        candidate_key=candidate.candidate_key,
        available=True,
        tickers=tickers,
    )


def _replay_stamp(
    *, reference_equity: float, max_position_risk_pct: float
) -> dict[str, object]:
    """Self-describing execution/sizing identity (F4) stamped onto every
    persisted replay payload, so a downstream reader never has to assume
    which engine version, sizing policy, order TTL, or side/fill scope
    produced a given row -- see the module docstring's RULING-A note for why
    ``EXECUTION_MODEL_VERSION`` is the current, re-versioned engine rather
    than the plan's original "exec-v3" name."""

    return {
        "execution_model_version": EXECUTION_MODEL_VERSION,
        "reference_equity": reference_equity,
        "max_position_risk_pct": max_position_risk_pct,
        "policy_fingerprint": TARGET_POLICY.policy_fingerprint,
        "order_ttl_minutes": _TTL_MINUTES,
        "side_scope": _SIDE_SCOPE,
        "fill_scope": _FILL_SCOPE,
    }


def case_replay_payload(
    evidence: CaseReplayEvidence,
    *,
    reference_equity: float = TARGET_POLICY.reference_equity,
    max_position_risk_pct: float = TARGET_POLICY.max_position_risk_pct,
) -> dict[str, object]:
    """Serialize one candidate's case replay for persistence alongside
    ``research_candidates.case_score_payload``. Every value is a JSON-safe
    primitive. ``reference_equity``/``max_position_risk_pct`` default to
    ``TARGET_POLICY``'s own values so an existing no-kwargs call site keeps
    stamping an accurate sizing identity unchanged; a caller that replayed
    with different sizing (e.g. ``replay_fold_candidates`` called with
    overrides) should pass the same values through here too."""

    return {
        "candidate_key": evidence.candidate_key,
        "available": evidence.available,
        "skip_reason": evidence.skip_reason,
        "realized_pnl": round(evidence.realized_pnl, 6),
        "filled_count": evidence.filled_count,
        "stamp": _replay_stamp(
            reference_equity=reference_equity,
            max_position_risk_pct=max_position_risk_pct,
        ),
        "tickers": [
            {
                "ticker": ticker.ticker,
                "status": ticker.status,
                "detail": ticker.detail,
                "side": ticker.side,
                "would_cross": ticker.would_cross,
                "limit_price": ticker.limit_price,
                "contracts": ticker.contracts,
                "fee_per_contract": ticker.fee_per_contract,
                "queue_ahead": ticker.queue_ahead,
                "probability": ticker.probability,
                "edge": ticker.edge,
                "edge_lcb": ticker.edge_lcb,
                "realized_pnl": round(ticker.realized_pnl, 6),
            }
            for ticker in evidence.tickers
        ],
    }


def replay_fold_candidates(
    fold: WalkForwardFold,
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
    config: StrategyConfig = StrategyConfig(),
    reference_equity: float = TARGET_POLICY.reference_equity,
    max_position_risk_pct: float = TARGET_POLICY.max_position_risk_pct,
) -> tuple[FoldReplayEvidence, ...]:
    """Replay both declared challengers, paired against the baseline, for a
    fold -- one ``FoldReplayEvidence`` row per challenger, mirroring
    ``research_candidates.score_fold_candidates``'s fold-grained shape.

    Reuses ``fit_fold_candidates`` (Task 3, unmodified) for the fitted
    ``(mu, sigma)`` per case/candidate; this function only ever replays
    money and never re-fits a distribution.
    """

    fitted = fit_fold_candidates(fold, shrinkage_k=shrinkage_k)
    station_id = fold.test[0].station_id
    target_date = fold.test[0].target_date

    challenger_keys = (GAUSSIAN_PIT_CANDIDATE_KEY, GOOGLE_RUNTIME_CANDIDATE_KEY)
    baseline_cases: dict[str, dict[str, dict[str, object]]] = {
        key: {} for key in challenger_keys
    }
    challenger_cases: dict[str, dict[str, dict[str, object]]] = {
        key: {} for key in challenger_keys
    }
    block_reasons: dict[str, set[str]] = {key: set() for key in challenger_keys}

    for case in fold.test:
        identity, gaussian, google = fitted[case.source_context_hash]
        baseline_evidence = replay_case_candidate(
            case,
            identity,
            config=config,
            reference_equity=reference_equity,
            max_position_risk_pct=max_position_risk_pct,
        )
        baseline_payload = case_replay_payload(
            baseline_evidence,
            reference_equity=reference_equity,
            max_position_risk_pct=max_position_risk_pct,
        )
        for challenger_candidate in (gaussian, google):
            key = challenger_candidate.candidate_key
            challenger_evidence = replay_case_candidate(
                case,
                challenger_candidate,
                config=config,
                reference_equity=reference_equity,
                max_position_risk_pct=max_position_risk_pct,
            )
            baseline_cases[key][case.source_context_hash] = baseline_payload
            challenger_cases[key][case.source_context_hash] = case_replay_payload(
                challenger_evidence,
                reference_equity=reference_equity,
                max_position_risk_pct=max_position_risk_pct,
            )
            if not baseline_evidence.available:
                block_reasons[key].add(
                    f"baseline replay unavailable for case "
                    f"{case.source_context_hash}: {baseline_evidence.skip_reason}"
                )
            if not challenger_evidence.available:
                block_reasons[key].add(
                    f"challenger replay unavailable for case "
                    f"{case.source_context_hash}: {challenger_evidence.skip_reason}"
                )
            # F1: a per-ticker invalid_market_entry outcome is corrupted
            # market data, not a legitimate no-trade result -- it must block
            # promotion exactly like a missing snapshot does, never pass
            # through silently just because *some other* ticker in the same
            # snapshot happened to parse cleanly.
            for role, replay_evidence in (
                ("baseline", baseline_evidence),
                ("challenger", challenger_evidence),
            ):
                for outcome in replay_evidence.tickers:
                    if outcome.status == "invalid_market_entry":
                        block_reasons[key].add(
                            f"{role} replay invalid_market_entry for case "
                            f"{case.source_context_hash} ticker {outcome.ticker}: "
                            f"{outcome.detail}"
                        )

    stamp = _replay_stamp(
        reference_equity=reference_equity, max_position_risk_pct=max_position_risk_pct
    )
    return tuple(
        FoldReplayEvidence(
            fold_id=fold.fold_id,
            station_id=station_id,
            target_date=target_date,
            challenger_candidate_key=key,
            baseline={"cases": baseline_cases[key], "stamp": stamp},
            challenger={"cases": challenger_cases[key], "stamp": stamp},
            promotion_eligible=not block_reasons[key],
            promotion_block_reasons=tuple(sorted(block_reasons[key])),
        )
        for key in challenger_keys
    )
