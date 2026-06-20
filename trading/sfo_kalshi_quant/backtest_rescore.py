"""Config-parameterized re-scoring backtest.

Replays the recorded ``decision_snapshots`` journal under a candidate
``StrategyConfig`` so a sizing/gate retune can be validated counterfactually.
The existing signal/market backtests only replay the OLD config's *recorded*
approve/size flags, so they structurally cannot answer "how would the retuned
gates + Kelly sizing have performed?" -- this module re-decides every historical
opportunity from scratch.

For each deduped, pre-resolution snapshot the rescorer:

1. reconstructs the ``MarketBin`` and a YES-frame ``BucketProbability`` from the
   persisted fields (inverting NO-side snapshots into the YES frame the engine
   expects, preserving the confidence band so a same-config re-run round-trips);
2. re-runs ``TradeEvaluator(config).evaluate_market(...)`` so every gate and the
   Kelly sizing run under the candidate config;
3. settles approved decisions against the official KSFO daily high with the
   after-fee kernel ``contracts * ((1 - cost) if won else -cost)`` (the entry
   fee is baked into ``cost_per_contract`` by the engine);
4. rolls the result up by INDEPENDENT WEATHER DAY -- one SFO daily high settles
   every bracket that day jointly, so the independent unit is the ``target_date``,
   not the contract.

The honest maximand is after-fee log-growth per independent day, NOT win-rate
(see docs/trade_engine_overhaul_plan_2026-06-17.md). Settlement is
held-to-settlement: a single entry snapshot cannot say whether/when the intraday
monitor would have closed a position early (that needs the monitor snapshots),
and hold-to-settlement is both the dominant exit and the EV reference.

Sizing uses a fixed ``bankroll`` (the reproducible control). The effect of
``size_against_live_equity`` is ~nil while equity is near the starting notional,
so a static bankroll keeps the rescore deterministic without materially changing
the verdict.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from .config import StrategyConfig, temperature_cohort
from .models import BucketProbability, MarketBin, TradeDecision
from .risk import TradeEvaluator

# Floor on a single day's gross return so a total-loss day (return == -1) does
# not send log(1 + r) to negative infinity in the log-growth average.
_RETURN_FLOOR = 1e-9


@dataclass(frozen=True)
class ReadinessThresholds:
    """The codified real-money go-live bar.

    Defaults are the project's own documented gate (docs/trade_engine_overhaul_
    plan_2026-06-17.md:91, docs/trading_retune_validation_2026-06-17.md:97):
    positive after-fee day-clustered ROI lower CI AND positive log-growth per
    independent day -- per side and per traded cohort -- over >=30 independent
    settlement days, with every traded cohort showing forecast SKILL (Brier Skill
    Score > 0, i.e. the model beats the climatological prior). A flat absolute
    Brier bar (the old max_cohort_brier=0.25) was unachievable on the interior
    2F bins by any calibrated model and is retained only for display context.
    """

    min_independent_days: int = 30
    min_settled_decisions: int = 30
    min_cohort_independent_days: int = 10
    min_side_independent_days: int = 10
    # The forecaster must BEAT climatology (skill > 0) on every traded cohort.
    # Strictly positive: skill == 0 means no edge over the no-skill prior.
    min_cohort_brier_skill: float = 0.0
    max_cohort_brier: float = 0.25  # retained for display/detail only
    max_calibration_gap: float = 0.10


def _opt_float(row: sqlite3.Row, key: str) -> float | None:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_side(row: sqlite3.Row) -> str:
    try:
        side = row["side"]
    except (IndexError, KeyError):
        side = None
    if side and str(side).upper() in {"YES", "NO"}:
        return str(side).upper()
    try:
        action = str(row["action"]).upper()
    except (IndexError, KeyError):
        return "YES"
    return "NO" if "NO" in action else "YES"


def reconstruct_market(row: sqlite3.Row) -> MarketBin:
    """Rebuild the ``MarketBin`` the engine saw, from a decision snapshot.

    The snapshot stores the YES book (``yes_bid``/``yes_ask``) plus the
    *traded-side* book in ``entry_bid``/``entry_ask`` (= the NO book for a NO
    row). We populate both sides so ``evaluate_market(side=...)`` reads the same
    prices the live scan did; NO-side depths go into ``raw`` as the
    ``no_*_size_fp`` overrides ``MarketBin`` honours.
    """

    side = _row_side(row)
    yes_bid = _opt_float(row, "yes_bid") or 0.0
    yes_ask = _opt_float(row, "yes_ask")
    yes_ask = 1.0 if yes_ask is None else yes_ask
    entry_bid = _opt_float(row, "entry_bid")
    entry_ask = _opt_float(row, "entry_ask")
    entry_bid_size = _opt_float(row, "entry_bid_size") or 0.0
    entry_ask_size = _opt_float(row, "entry_ask_size") or 0.0

    raw: dict[str, object] = {}
    close_time = None
    try:
        close_time = row["market_close_time"]
    except (IndexError, KeyError):
        close_time = None
    if close_time:
        raw["close_time"] = close_time

    if side == "NO":
        no_bid = entry_bid if entry_bid is not None else max(0.0, 1.0 - yes_ask)
        no_ask = entry_ask if entry_ask is not None else max(0.0, 1.0 - yes_bid)
        # The engine reads side_bid_size/side_ask_size for NO from these raw
        # overrides; without them it would fall back to the YES sizes.
        raw["no_bid_size_fp"] = entry_bid_size
        raw["no_ask_size_fp"] = entry_ask_size
        yes_bid_size = 0.0
        yes_ask_size = 0.0
    else:
        no_bid = max(0.0, 1.0 - yes_ask)
        no_ask = max(0.0, 1.0 - yes_bid)
        yes_bid_size = entry_bid_size
        yes_ask_size = entry_ask_size

    return MarketBin(
        ticker=str(row["market_ticker"]),
        event_ticker=str(_row_value(row, "event_ticker", "")),
        title="",
        yes_sub_title=str(_row_value(row, "label", "")),
        strike_type=str(_row_value(row, "strike_type", "")),
        floor_strike=_opt_float(row, "floor_strike"),
        cap_strike=_opt_float(row, "cap_strike"),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        status=str(_row_value(row, "market_status", "active")) or "active",
        raw=raw,
    )


def _row_value(row: sqlite3.Row, key: str, default):
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _to_yes_frame(side_value: float | None, side: str) -> float | None:
    """Invert a side-frame probability back to the YES frame (NO -> 1 - p)."""

    if side_value is None:
        return None
    if side == "YES":
        return side_value
    return max(0.0, min(1.0, 1.0 - side_value))


def reconstruct_probability(row: sqlite3.Row) -> BucketProbability:
    """Rebuild the YES-frame ``BucketProbability`` from a decision snapshot.

    Snapshots store the *traded-side* probabilities (a NO row carries NO-frame
    values). ``evaluate_market`` expects YES-frame inputs and converts to the
    side internally, so we invert NO rows. The point inversion is ``1 - p``; the
    lower bound is rebuilt from the recorded confidence band width
    (``p - lcb``), which the engine's YES->NO transform preserves -- so feeding
    the YES frame back through ``evaluate_market(side="NO")`` reproduces the
    recorded NO probability and NO LCB exactly.
    """

    side = _row_side(row)
    side_prob = float(row["probability"])
    side_lcb = float(row["probability_lcb"])
    uncertainty = max(0.0, side_prob - side_lcb)

    if side == "YES":
        yes_prob = side_prob
        yes_lcb = side_lcb
    else:
        yes_prob = max(0.0, min(1.0, 1.0 - side_prob))
        # Preserve the band width: YES_lcb = YES_prob - uncertainty.
        yes_lcb = yes_prob - uncertainty

    return BucketProbability(
        ticker=str(row["market_ticker"]),
        label=str(_row_value(row, "label", "")),
        probability=yes_prob,
        lower_confidence=yes_lcb,
        # empirical/normal/effective_n are calibrator internals; the engine does
        # not read them for gating or sizing, so reconstructing the YES point is
        # sufficient.
        empirical_probability=yes_prob,
        normal_probability=yes_prob,
        effective_n=0.0,
        residual_probability=_to_yes_frame(_opt_float(row, "residual_probability"), side),
        ensemble_probability=_to_yes_frame(_opt_float(row, "ensemble_probability"), side),
        model_probability=_to_yes_frame(_opt_float(row, "model_probability"), side),
        market_probability=_to_yes_frame(_opt_float(row, "market_probability"), side),
        observed_high_f=_opt_float(row, "intraday_observed_high_f"),
        intraday_probability=_to_yes_frame(_opt_float(row, "intraday_probability"), side),
        remaining_heat_risk=_opt_float(row, "remaining_heat_risk"),
    )


def rescore_row(row: sqlite3.Row, config: StrategyConfig, *, bankroll: float) -> TradeDecision:
    """Re-decide one snapshot under ``config`` (gates + Kelly run from scratch)."""

    market = reconstruct_market(row)
    probability = reconstruct_probability(row)
    # market_consensus is intentionally omitted: the full bid/ask ladder is not
    # stored per snapshot, so the consensus guard's SIZE haircut cannot be
    # replayed here (it no-ops). The rescore re-runs only gates + Kelly on the
    # STORED posterior; the anchor's heavier blend weight is whatever was baked
    # into that recorded probability at decision time, not re-derived under
    # ``config`` -- so both anchor and guard effects must be validated from the
    # live research book, not reconstructed here.
    return TradeEvaluator(config).evaluate_market(
        market,
        probability,
        bankroll=bankroll,
        side=_row_side(row),
        source_spread_f=_opt_float(row, "forecast_source_spread_f"),
        forecast_high_f=_opt_float(row, "forecast_predicted_high_f"),
    )


@dataclass(frozen=True)
class _Scored:
    target_date: str
    side: str
    settlement_high_f: float
    # cohort = settled-high regime (for calibration/diagnostics). forecast_cohort
    # = the regime the live block actually gated on at entry time (it only knows
    # the forecast, not the settled high). Readiness must scope "traded cohort" by
    # forecast_cohort so a forecast-normal day that settles warm does not become a
    # "traded warm cohort" demanding a warm skill the block was meant to avoid.
    cohort: str
    forecast_cohort: str
    contracts: float
    cost: float
    capital: float
    pnl: float
    won: bool


def _integer_settlement_high_f(high: float) -> float:
    """Round a (possibly fractional) ground-truth high to the integer Kalshi
    settles off (the NWS Daily Climate Report value). Half-up, matching
    backtest.py / synthetic_blend.py so resolution is consistent repo-wide."""

    return float(math.floor(high + 0.5))


def _recorded_pnl(row: sqlite3.Row, won: bool) -> tuple[float, float]:
    """(capital, pnl) the recorded config booked for this row, after fees."""

    contracts = _opt_float(row, "recommended_contracts") or 0.0
    cost = _opt_float(row, "cost_per_contract") or 0.0
    capital = contracts * cost
    pnl = contracts * ((1.0 - cost) if won else -cost)
    return capital, pnl


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = pct / 100.0 * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _day_clustered_roi_ci(
    per_day: dict[str, dict[str, float]],
    *,
    samples: int,
    seed: int,
) -> tuple[float, float] | None:
    """Bootstrap a 95% CI on portfolio ROI by resampling INDEPENDENT DAYS.

    Same-day brackets settle jointly, so resampling contracts would understate
    variance; resampling whole days is the honest unit. Deterministic for a
    given seed so the report is reproducible.
    """

    days = [d for d in per_day.values() if d["capital"] > 0]
    if len(days) < 2:
        return None
    import random

    rng = random.Random(seed)
    n = len(days)
    rois: list[float] = []
    for _ in range(samples):
        pnl = 0.0
        capital = 0.0
        for _ in range(n):
            day = days[rng.randrange(n)]
            pnl += day["pnl"]
            capital += day["capital"]
        if capital > 0:
            rois.append(pnl / capital)
    if not rois:
        return None
    return (_percentile(rois, 2.5), _percentile(rois, 97.5))


def _bucket(rows: list[_Scored]) -> dict[str, object]:
    """Aggregate a list of scored decisions into a metrics block."""

    trades = len(rows)
    days = sorted({r.target_date for r in rows})
    wins = sum(1 for r in rows if r.won)
    losses = trades - wins
    capital = sum(r.capital for r in rows)
    pnl = sum(r.pnl for r in rows)
    return {
        "trades": trades,
        "independent_days": len(days),
        "wins": wins,
        "losses": losses,
        "capital_at_risk": round(capital, 4),
        "realized_pnl": round(pnl, 4),
        "roi": round(pnl / capital, 6) if capital > 0 else None,
        "hit_rate": round(wins / trades, 4) if trades else None,
    }


def run_rescore(
    rows: list[sqlite3.Row],
    settlements: dict[object, float],
    config: StrategyConfig,
    *,
    bankroll: float,
    bootstrap_samples: int = 2000,
    seed: int = 0,
) -> dict[str, object]:
    """Re-score ``rows`` under ``config`` and roll up by independent weather day."""

    normalized = {str(key): float(value) for key, value in settlements.items()}
    evaluator = TradeEvaluator(config)
    scored: list[_Scored] = []
    considered = 0
    approved_under_config = 0
    approved_under_recorded = 0
    approved_no_settlement = 0
    # The recorded config's OWN full settled book (every recorded-approved,
    # settled row -- including ones the candidate rejected), so the comparison is
    # strategy-vs-strategy rather than the candidate-approved subset.
    recorded_pnl = 0.0
    recorded_capital = 0.0
    recorded_wins = 0
    recorded_trades = 0
    recorded_days: set[str] = set()

    for row in rows:
        considered += 1
        try:
            recorded_approved = bool(int(row["approved"]))
        except (IndexError, KeyError, TypeError, ValueError):
            recorded_approved = False
        if recorded_approved:
            approved_under_recorded += 1

        market = reconstruct_market(row)
        probability = reconstruct_probability(row)
        side = _row_side(row)
        decision = evaluator.evaluate_market(
            market,
            probability,
            bankroll=bankroll,
            side=side,
            source_spread_f=_opt_float(row, "forecast_source_spread_f"),
            forecast_high_f=_opt_float(row, "forecast_predicted_high_f"),
            # Same comfort-edge uncertainty proxy as the live analyze path, so the
            # rescore evaluates the exact band the engine would have used.
            forecast_sigma_f=_opt_float(row, "forecast_source_spread_f"),
        )
        candidate_approved = decision.approved and decision.recommended_contracts > 0
        if candidate_approved:
            approved_under_config += 1

        target_date = str(row["target_date"])
        if target_date not in normalized:
            if candidate_approved:
                approved_no_settlement += 1
            continue

        # Kalshi settles off the INTEGER NWS daily climate value; the stored
        # ground-truth high may be fractional, so round half-up to the integer
        # bin the contract actually resolves against.
        settlement_high = _integer_settlement_high_f(normalized[target_date])
        # Win/loss depends only on the bin, the side, and the settled high -- the
        # same for the candidate and recorded config -- so compute it once.
        resolved_yes = market.resolves_yes(settlement_high)
        won = resolved_yes if side == "YES" else not resolved_yes

        if candidate_approved:
            contracts = float(decision.recommended_contracts)
            cost = float(decision.cost_per_contract)
            capital = contracts * cost
            pnl = contracts * ((1.0 - cost) if won else -cost)
            forecast_high = _opt_float(row, "forecast_predicted_high_f")
            scored.append(
                _Scored(
                    target_date=target_date,
                    side=side,
                    settlement_high_f=settlement_high,
                    cohort=temperature_cohort(settlement_high),
                    # The regime the live block gated on. Fall back to the settled
                    # cohort only when the row carries no forecast high (legacy
                    # rows); fail-closed keeps such a row in its settled cohort.
                    forecast_cohort=(
                        temperature_cohort(forecast_high)
                        if forecast_high is not None
                        else temperature_cohort(settlement_high)
                    ),
                    contracts=contracts,
                    cost=cost,
                    capital=capital,
                    pnl=pnl,
                    won=won,
                )
            )

        if recorded_approved:
            rec_capital, rec_pnl = _recorded_pnl(row, won)
            recorded_pnl += rec_pnl
            recorded_capital += rec_capital
            recorded_trades += 1
            recorded_wins += 1 if won else 0
            recorded_days.add(target_date)

    return _summarize(
        scored,
        considered=considered,
        approved_under_config=approved_under_config,
        approved_under_recorded=approved_under_recorded,
        approved_no_settlement=approved_no_settlement,
        starting_bankroll=bankroll,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        recorded={
            "realized_pnl": recorded_pnl,
            "capital_at_risk": recorded_capital,
            "wins": recorded_wins,
            "trades": recorded_trades,
            "independent_days": len(recorded_days),
        },
    )


def _summarize(
    scored: list[_Scored],
    *,
    considered: int,
    approved_under_config: int,
    approved_under_recorded: int,
    approved_no_settlement: int,
    starting_bankroll: float,
    bootstrap_samples: int,
    seed: int,
    recorded: dict[str, float],
) -> dict[str, object]:
    settled = scored
    per_day: dict[str, dict[str, float]] = {}
    for r in settled:
        day = per_day.setdefault(
            r.target_date, {"pnl": 0.0, "capital": 0.0, "wins": 0.0, "trades": 0.0}
        )
        day["pnl"] += r.pnl
        day["capital"] += r.capital
        day["wins"] += 1.0 if r.won else 0.0
        day["trades"] += 1.0

    total_pnl = sum(r.pnl for r in settled)
    total_capital = sum(r.capital for r in settled)
    independent_days = sorted(per_day.keys())

    # Day-level returns for log-growth per independent day.
    day_returns = [
        d["pnl"] / d["capital"] for d in per_day.values() if d["capital"] > 0
    ]
    log_growth_per_day = None
    geometric_growth_per_day = None
    if day_returns:
        log_growth_per_day = sum(
            math.log(max(_RETURN_FLOOR, 1.0 + r)) for r in day_returns
        ) / len(day_returns)
        geometric_growth_per_day = math.exp(log_growth_per_day) - 1.0

    ci = _day_clustered_roi_ci(per_day, samples=bootstrap_samples, seed=seed)

    # Recorded-config book, computed independently over EVERY recorded-approved
    # settled row (passed in), so this is the recorded strategy's own after-fee
    # book -- a true strategy-vs-strategy comparison, not the candidate subset.
    recorded_pnl = float(recorded["realized_pnl"])
    recorded_capital = float(recorded["capital_at_risk"])

    cohorts: dict[str, list[_Scored]] = {}
    for r in settled:
        cohorts.setdefault(r.cohort, []).append(r)
    forecast_cohorts: dict[str, list[_Scored]] = {}
    for r in settled:
        forecast_cohorts.setdefault(r.forecast_cohort, []).append(r)
    sides: dict[str, list[_Scored]] = {}
    for r in settled:
        sides.setdefault(r.side, []).append(r)

    wins = sum(1 for r in settled if r.won)
    settled_trades = len(settled)

    return {
        "config_basis": "paper-realism, not real-money validated",
        "starting_bankroll": round(starting_bankroll, 2),
        "counts": {
            "considered": considered,
            "approved_under_recorded_config": approved_under_recorded,
            "approved_under_candidate_config": approved_under_config,
            "approved_without_settlement": approved_no_settlement,
            "settled_decisions": settled_trades,
            "independent_days": len(independent_days),
        },
        "candidate": {
            "realized_pnl": round(total_pnl, 4),
            "capital_at_risk": round(total_capital, 4),
            "roi": round(total_pnl / total_capital, 6) if total_capital > 0 else None,
            "wins": wins,
            "losses": settled_trades - wins,
            "hit_rate_per_trade": round(wins / settled_trades, 4) if settled_trades else None,
            "ending_equity": round(starting_bankroll + total_pnl, 2),
            "log_growth_per_independent_day": (
                round(log_growth_per_day, 6) if log_growth_per_day is not None else None
            ),
            "geometric_growth_per_independent_day": (
                round(geometric_growth_per_day, 6)
                if geometric_growth_per_day is not None
                else None
            ),
            "roi_ci95_day_clustered": (
                [round(ci[0], 6), round(ci[1], 6)] if ci is not None else None
            ),
        },
        "recorded_config_own_book": {
            "realized_pnl": round(recorded_pnl, 4),
            "capital_at_risk": round(recorded_capital, 4),
            "roi": round(recorded_pnl / recorded_capital, 6) if recorded_capital > 0 else None,
            "settled_decisions": int(recorded["trades"]),
            "independent_days": int(recorded["independent_days"]),
            "wins": int(recorded["wins"]),
            "hit_rate_per_trade": (
                round(recorded["wins"] / recorded["trades"], 4) if recorded["trades"] else None
            ),
        },
        "by_cohort": {name: _bucket(rows) for name, rows in sorted(cohorts.items())},
        # Keyed by the forecast-time regime the live block actually gated on, so
        # readiness scopes "traded cohort" to what the engine chose to trade.
        "by_forecast_cohort": {
            name: _bucket(rows) for name, rows in sorted(forecast_cohorts.items())
        },
        "by_side": {name: _bucket(rows) for name, rows in sorted(sides.items())},
        "per_day": [
            {
                "target_date": day,
                "realized_pnl": round(per_day[day]["pnl"], 4),
                "capital_at_risk": round(per_day[day]["capital"], 4),
                "trades": int(per_day[day]["trades"]),
                "wins": int(per_day[day]["wins"]),
                "roi": (
                    round(per_day[day]["pnl"] / per_day[day]["capital"], 6)
                    if per_day[day]["capital"] > 0
                    else None
                ),
            }
            for day in independent_days
        ],
    }


def _readiness_check(
    name: str,
    label: str,
    passed: bool,
    detail: str,
    progress: float | None = None,
) -> dict[str, object]:
    # progress (0..1) is how far this check is toward passing -- count-based
    # checks supply a ratio so the overall gauge climbs as data accumulates;
    # boolean checks fall back to 0/1. Passing always means full progress.
    if progress is None:
        progress = 1.0 if passed else 0.0
    if passed:
        progress = 1.0
    return {
        "name": name,
        "label": label,
        "passed": bool(passed),
        "progress": round(max(0.0, min(1.0, progress)), 4),
        "detail": detail,
    }


def compute_real_money_readiness(
    rescore: dict[str, object],
    *,
    calibration_cohort_brier: dict[str, float] | None = None,
    calibration_cohort_brier_skill: dict[str, float] | None = None,
    max_abs_calibration_gap: float | None = None,
    weighted_calibration_ece: float | None = None,
    thresholds: ReadinessThresholds | None = None,
) -> dict[str, object]:
    """Collapse the LIVE-profile rescore (+ calibration) into a go/no-go verdict.

    Pure: consumes the ``run_rescore`` output for the live profile plus the
    walk-forward per-cohort Brier Skill Score and the worst calibration-bucket
    gap. Every check is blocking and fails CLOSED -- a missing input cannot read
    as a pass, so the verdict only flips to READY when every documented
    precondition is simultaneously satisfied on real settled data. Per-cohort and
    per-side checks are scoped to the FORECAST cohort (what the engine chose to
    trade), so blocked regimes are never demanded.
    """

    thresholds = thresholds or ReadinessThresholds()
    counts = rescore.get("counts") or {}
    candidate = rescore.get("candidate") or {}
    # Scope per-cohort checks to the forecast-time regime the live block gated on;
    # fall back to the settled-high rollup only if the rescore predates the split.
    by_cohort = rescore.get("by_forecast_cohort") or rescore.get("by_cohort") or {}
    by_settled_cohort = rescore.get("by_cohort") or {}
    by_side = rescore.get("by_side") or {}

    checks: list[dict[str, object]] = []

    independent_days = int(counts.get("independent_days") or 0)
    checks.append(
        _readiness_check(
            "independent_days",
            "Independent settlement days",
            independent_days >= thresholds.min_independent_days,
            f"{independent_days}/{thresholds.min_independent_days} independent weather days",
            progress=independent_days / thresholds.min_independent_days
            if thresholds.min_independent_days
            else 1.0,
        )
    )

    settled = int(counts.get("settled_decisions") or 0)
    checks.append(
        _readiness_check(
            "settled_decisions",
            "Settled decisions",
            settled >= thresholds.min_settled_decisions,
            f"{settled}/{thresholds.min_settled_decisions} settled decisions",
            progress=settled / thresholds.min_settled_decisions
            if thresholds.min_settled_decisions
            else 1.0,
        )
    )

    ci = candidate.get("roi_ci95_day_clustered")
    ci_lo = ci[0] if isinstance(ci, (list, tuple)) and len(ci) == 2 and ci[0] is not None else None
    checks.append(
        _readiness_check(
            "after_fee_roi_lower_ci_positive",
            "After-fee ROI lower CI > 0",
            ci_lo is not None and ci_lo > 0,
            f"day-clustered 95% ROI lower bound {ci_lo:+.2%}" if ci_lo is not None
            else "no day-clustered ROI CI (too few independent days)",
        )
    )

    log_growth = candidate.get("log_growth_per_independent_day")
    checks.append(
        _readiness_check(
            "positive_log_growth_per_day",
            "Positive after-fee log-growth/day",
            log_growth is not None and log_growth > 0,
            f"log-growth {log_growth:+.4f}/day" if log_growth is not None
            else "no settled days to measure growth",
        )
    )

    # Per-traded-cohort calibration: the forecaster must show SKILL (beat the
    # climatological prior, Brier Skill Score > 0) on every regime the live book
    # actually trades. Skill -- not absolute Brier -- is the honest bar: a flat
    # Brier threshold penalizes irreducible multi-bin spread and is unreachable
    # on the interior 2F bins by any calibrated model.
    traded_cohorts = [name for name, b in by_cohort.items() if int(b.get("trades") or 0) > 0]
    if not traded_cohorts:
        checks.append(
            _readiness_check(
                "cohort_skill", "Traded-cohort forecast skill",
                False, "no traded cohorts yet",
            )
        )
    for cohort in sorted(traded_cohorts):
        skill = (calibration_cohort_brier_skill or {}).get(cohort)
        brier = (calibration_cohort_brier or {}).get(cohort)
        brier_note = f" (Brier {brier:.3f})" if brier is not None else ""
        checks.append(
            _readiness_check(
                f"cohort_skill:{cohort}",
                f"Forecast skill > {thresholds.min_cohort_brier_skill:g} ({cohort})",
                skill is not None and skill > thresholds.min_cohort_brier_skill,
                f"Brier Skill Score {skill:+.3f}{brier_note}" if skill is not None
                else "walk-forward cohort skill unavailable",
            )
        )

    # No single regime or side may carry the verdict: every traded cohort and
    # side needs breadth AND its OWN positive after-fee ROI (not just the
    # portfolio aggregate, which can hide a quietly losing regime/side).
    for cohort in sorted(traded_cohorts):
        bucket = by_cohort.get(cohort) or {}
        days = int(bucket.get("independent_days") or 0)
        floor = thresholds.min_cohort_independent_days
        checks.append(
            _readiness_check(
                f"cohort_days:{cohort}",
                f"Cohort breadth ({cohort})",
                days >= floor,
                f"{days}/{floor} independent days",
                progress=days / floor if floor else 1.0,
            )
        )
        roi = bucket.get("roi")
        checks.append(
            _readiness_check(
                f"cohort_roi:{cohort}",
                f"Cohort after-fee ROI > 0 ({cohort})",
                roi is not None and roi > 0,
                f"ROI {roi:+.2%}" if roi is not None else "no settled ROI",
            )
        )
    for side in sorted(name for name, b in by_side.items() if int(b.get("trades") or 0) > 0):
        bucket = by_side.get(side) or {}
        days = int(bucket.get("independent_days") or 0)
        floor = thresholds.min_side_independent_days
        checks.append(
            _readiness_check(
                f"side_days:{side}",
                f"Side breadth ({side})",
                days >= floor,
                f"{days}/{floor} independent days",
                progress=days / floor if floor else 1.0,
            )
        )
        roi = bucket.get("roi")
        checks.append(
            _readiness_check(
                f"side_roi:{side}",
                f"Side after-fee ROI > 0 ({side})",
                roi is not None and roi > 0,
                f"ROI {roi:+.2%}" if roi is not None else "no settled ROI",
            )
        )

    # The gate stays the worst single-bucket gap (fail-closed), but the
    # sample-weighted ECE is surfaced beside it because the max-gap is noisy when
    # the driving bucket is sparse (a 14-sample mid-confidence bucket can swing the
    # gap +/-0.15 on resampling). ECE is the stable companion signal; it is
    # reported, NOT used to loosen the gate.
    if max_abs_calibration_gap is None:
        gap_detail = "calibration gap unavailable"
    else:
        gap_detail = f"max bucket gap {max_abs_calibration_gap:.3f}"
        if weighted_calibration_ece is not None:
            gap_detail += f" (sample-weighted ECE {weighted_calibration_ece:.3f})"
    checks.append(
        _readiness_check(
            "calibration_gap",
            f"Calibration gap < {thresholds.max_calibration_gap:g}",
            max_abs_calibration_gap is not None
            and max_abs_calibration_gap < thresholds.max_calibration_gap,
            gap_detail,
        )
    )

    passed = sum(1 for c in checks if c["passed"])
    ready = passed == len(checks)
    # "How ready" gauge: the mean per-check progress, so it climbs smoothly as
    # data accumulates instead of stepping only when a check flips. It can only
    # reach 100% when every check actually passes (ready == True).
    readiness_pct = round(100.0 * sum(c["progress"] for c in checks) / len(checks), 1) if checks else 0.0
    if readiness_pct >= 100.0 and not ready:
        readiness_pct = 99.0
    return {
        "ready": ready,
        "verdict": "READY" if ready else "NOT READY",
        "readiness_pct": readiness_pct,
        "checks_passed": passed,
        "checks_total": len(checks),
        "summary": (
            f"{readiness_pct:.0f}% ready for real money -- "
            f"{passed}/{len(checks)} go-live checks pass"
        ),
        "checks": checks,
        "thresholds": {
            "min_independent_days": thresholds.min_independent_days,
            "min_settled_decisions": thresholds.min_settled_decisions,
            "min_cohort_independent_days": thresholds.min_cohort_independent_days,
            "min_side_independent_days": thresholds.min_side_independent_days,
            "min_cohort_brier_skill": thresholds.min_cohort_brier_skill,
            "max_cohort_brier": thresholds.max_cohort_brier,
            "max_calibration_gap": thresholds.max_calibration_gap,
        },
        "config_basis": rescore.get("config_basis"),
    }
