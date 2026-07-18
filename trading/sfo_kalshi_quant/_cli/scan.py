"""Forecast/market scan orchestration behind the stable CLI facade."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.error import URLError

from ..arbitrage import build_arbitrage_opportunities
from ..cities import CityConfig, get_city, parse_city_slugs
from ..colors import Color
from ..config import (
    SERIES_TICKER,
    StrategyConfig,
    config_for_city,
    intraday_timezone_for_city,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from ..consensus import MarketConsensus, build_market_consensus
from ..db import PaperStore
from ..ensemble import OpenMeteoEnsembleError, SfoEnsembleClient
from ..forecast import (
    ForecastDataError,
    SfoForecasterAdapter,
    has_forecaster_observed_high_adjustment,
    parse_target_date,
    parse_target_dates,
)
from ..kalshi import KalshiPublicClient, load_event_snapshots
from ..models import (
    BucketProbability,
    EnsembleSnapshot,
    EventSnapshot,
    ForecastOutcome,
    ForecastSnapshot,
    IntradaySnapshot,
    MarketBin,
    TradeDecision,
    format_event_date_token,
)
from ..paper import ArbitrageContainmentError, PaperTrader
from ..portfolio import PortfolioPlan, allocate_portfolio
from ..posterior_kelly import load_posterior_kelly_model
from ..probability import ResidualCalibrator
from ..research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    canonical_research_lead_bucket,
)
from ..research_portfolio import ResearchOpportunity, ResearchPlans, allocate_research_plans
from ..risk import TradeEvaluator
from ..settlement_day import settlement_clock, settlement_today
from ..standard_bins import fallback_bins
from ..tail_basket import build_tail_basket
from .format import (
    _print_analysis,
    _print_arbitrage,
    _print_portfolio_scan,
    _print_tail_basket,
)


DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR = 14
_UNSET = object()


# These helpers are supplied by the CLI facade when it dispatches into this
# module. Defaults keep the scan engine directly importable and testable.
def _risk_profile_name(args: argparse.Namespace) -> str:
    explicit = getattr(args, "risk_profile", None)
    return normalize_risk_profile_name(str(explicit) if explicit else None)


def _analysis_sides(side_arg: str) -> tuple[str, ...]:
    if side_arg == "both":
        return ("YES", "NO")
    return (side_arg.upper(),)


def _default_calibration_source() -> str:
    return os.getenv("SFO_TRADING_SIGNAL_CALIBRATION_SOURCE", "lstm")


def _enforce_live_forecast_freshness(forecast, config: StrategyConfig) -> None:
    today = parse_target_date("today")
    if forecast.target_date < today:
        return
    age_hours = forecast.age_hours()
    if age_hours is None:
        raise ForecastDataError("forecast snapshot has no readable fetched_at timestamp")
    if age_hours > config.max_forecast_age_hours:
        raise ForecastDataError(
            f"forecast snapshot for {forecast.target_date.isoformat()} is stale "
            f"({age_hours:.1f}h old; max {config.max_forecast_age_hours:.1f}h)"
        )


def _resolve_analysis_targets(
    args: argparse.Namespace,
    color: Color,
    kalshi_client: KalshiPublicClient,
    city: CityConfig | None = None,
) -> tuple[list[date], dict[date, EventSnapshot]]:
    series_ticker = city.series_ticker if city is not None else SERIES_TICKER
    clock_targets = parse_target_dates(args.target_date)
    if args.offline_events or args.target_date != "rolling":
        return clock_targets, {}

    try:
        events = kalshi_client.list_event_snapshots(
            series_ticker=series_ticker,
            limit=20,
            with_nested_markets=True,
        )
    except (URLError, OSError) as exc:
        if args.place_paper:
            print(
                color.yellow(
                    f"warning: live Kalshi rolling target lookup failed ({exc}); "
                    "skipping paper scan instead of using clock-derived target dates"
                ),
                file=sys.stderr,
            )
            return [], {}
        print(
            color.yellow(
                f"warning: live Kalshi rolling target lookup failed ({exc}); "
                "using clock-derived probability targets"
            ),
            file=sys.stderr,
        )
        return clock_targets, {}

    targets, events_by_target = _rolling_live_event_targets(events, city=city)
    if targets:
        return targets, events_by_target

    if args.place_paper:
        print(
            color.yellow(
                f"warning: no active Kalshi {series_ticker} events found; skipping paper "
                "scan instead of using clock-derived target dates"
            ),
            file=sys.stderr,
        )
        return [], {}
    return clock_targets, {}


def _clamp_sizing_equity(equity: float, starting_bankroll: float) -> float:
    """Bound compounding to [0.5x, 2x] of the starting notional.

    Kelly sizes off current wealth, so a winning book should stake more and a
    losing one less. But on a tiny, noisy paper sample an early lucky (or unlucky)
    run would balloon (or zero) stakes off pure variance. Clamp the equity used
    for sizing so a drawdown cannot collapse stakes to nothing and a hot streak
    cannot run away before the sample is meaningful.
    """

    return min(max(equity, 0.5 * starting_bankroll), 2.0 * starting_bankroll)


def _sizing_bankroll(store: PaperStore, config: StrategyConfig, risk_profile: str | None) -> float:
    """Bankroll used for Kelly and the risk caps.

    Frozen notional by default (reproducible paper runs); live realized equity
    (clamped) when size_against_live_equity is set, so sizing fractions current
    wealth without letting a noisy early sample run away with the stake.
    """

    if config.size_against_live_equity:
        equity = store.paper_equity(config.paper_bankroll, risk_profile=risk_profile)
        return _clamp_sizing_equity(equity, config.paper_bankroll)
    return config.paper_bankroll


def _build_sizing_model(config: StrategyConfig, store: PaperStore):
    """Posterior-mean Kelly model (Phase 2b) from the settled journal, or None
    when the profile has it disabled -- the frozen-baseline, no-op path."""

    if not config.posterior_mean_kelly_enabled:
        return None
    with store.connect() as conn:
        return load_posterior_kelly_model(
            conn,
            prior_strength=config.posterior_mean_kelly_prior_strength,
            floor=config.posterior_mean_kelly_floor,
            min_cohort_n=config.posterior_mean_kelly_min_cohort_n,
        )


def _cached_paper_entry_pause_reason(
    store: PaperStore,
    risk_profile: str,
    *,
    bankroll: float,
    target_date: str,
    cache: dict[tuple[str, str], str | None] | None,
) -> str | None:
    key = (risk_profile, target_date)
    if cache is not None and key in cache:
        return cache[key]
    reason = store.paper_entry_pause_reason(
        risk_profile,
        bankroll=bankroll,
        target_date=target_date,
    )
    if cache is not None:
        cache[key] = reason
    return reason


def _rolling_targets_count() -> int:
    # Kalshi lists SFO events several days out; scanning more of them grows the
    # distinct-candidate universe (and the paper sample) without touching any
    # edge gate. Bounded so a misconfig can't fan out unboundedly.
    raw = os.getenv("PAPER_ROLLING_TARGETS", "3")
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, min(value, 7))


def _rolling_live_event_targets(
    events: list[EventSnapshot],
    *,
    now: datetime | None = None,
    max_targets: int | None = None,
    city: CityConfig | None = None,
) -> tuple[list[date], dict[date, EventSnapshot]]:
    if max_targets is None:
        max_targets = _rolling_targets_count()
    local_now = settlement_clock(now, city)
    today = local_now.date()
    min_target = today
    if local_now.hour >= _same_day_entry_cutoff_hour():
        min_target = today + timedelta(days=1)
    events_by_target: dict[date, EventSnapshot] = {}
    for event in events:
        target = event.target_date
        if target is None or target < min_target or not event.active_markets:
            continue
        current = events_by_target.get(target)
        if current is None or len(event.active_markets) > len(current.active_markets):
            events_by_target[target] = event
    targets = sorted(events_by_target)[:max_targets]
    return targets, {target: events_by_target[target] for target in targets}


@dataclass
class ScanContext:
    city: CityConfig
    series_ticker: str
    forecast: ForecastSnapshot
    intraday: IntradaySnapshot | None
    ensemble: EnsembleSnapshot | None
    event: EventSnapshot | None
    markets: list[MarketBin]
    event_title: str
    market_available: bool
    probabilities: dict[str, BucketProbability]
    consensus: MarketConsensus
    risk_profile: str
    paper_bankroll: float
    decisions: list[TradeDecision]


def build_scan_context(
    args: argparse.Namespace,
    target: date,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    city: CityConfig | None = None,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
    emos_lookup: dict | None = None,
    sizing_model=_UNSET,
    fallback_event_title: str,
) -> ScanContext:
    """Build the common forecast/market/probability context for both scan modes."""

    city = city or get_city("sfo")
    series_ticker = city.series_ticker
    forecast = adapter.latest_blend(target)
    _enforce_live_forecast_freshness(forecast, config)
    intraday = _intraday_for_target(args, target, adapter, city=city)
    observed_high_f = intraday.observed_high_f if intraday else None
    if intraday is not None and not has_forecaster_observed_high_adjustment(forecast):
        forecast = adapter.apply_intraday_update(forecast, intraday)
    # Ensemble sharpening is SFO-validated and too quota-expensive for all cities.
    ensemble = (
        _ensemble_for_target(args, target, forecast.predicted_high_f, color, city=city)
        if city.has_full_blend
        else None
    )
    event = event_hint
    if event_lookup_done:
        pass
    elif args.offline_events:
        events = load_event_snapshots(args.offline_events, target)
        event = events[0] if events else None
    else:
        try:
            client = kalshi_client or KalshiPublicClient()
            event = client.find_event_by_date(target, series_ticker=series_ticker)
        except (URLError, OSError) as exc:
            print(
                color.yellow(
                    f"warning: live Kalshi lookup failed ({exc}); using probability-only ladder"
                ),
                file=sys.stderr,
            )
            event = None

    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
    else:
        markets = fallback_bins(
            f"{series_ticker}-{format_event_date_token(target)}-PAPER",
            forecast.predicted_high_f,
        )
        event_title = fallback_event_title

    if emos_lookup is None:
        emos_lookup = (
            adapter.load_emos_mu_sigma(lead_days=None)
            if config.emos_distribution_enabled
            else {}
        )
    probabilities = calibrator.bucket_probabilities(
        markets,
        forecast.predicted_high_f,
        source_spread_f=forecast.source_spread_f,
        observed_high_f=observed_high_f,
        ensemble=ensemble,
        intraday=intraday,
        emos_mu_sigma=emos_lookup.get(target),
        standard_timezone=intraday_timezone_for_city(city),
    )
    consensus = build_market_consensus(markets)
    risk_profile = _risk_profile_name(args)
    paper_bankroll = _sizing_bankroll(store, config, risk_profile)
    evaluator = TradeEvaluator(
        config,
        sizing_model=(
            _build_sizing_model(config, store) if sizing_model is _UNSET else sizing_model
        ),
    )
    decisions = evaluator.rank(
        markets,
        probabilities,
        bankroll=paper_bankroll,
        sides=_analysis_sides(args.side),
        source_spread_f=forecast.source_spread_f,
        forecast_high_f=forecast.predicted_high_f,
        forecast_sigma_f=forecast.source_spread_f,
        market_consensus=consensus,
    )
    return ScanContext(
        city=city,
        series_ticker=series_ticker,
        forecast=forecast,
        intraday=intraday,
        ensemble=ensemble,
        event=event,
        markets=markets,
        event_title=event_title,
        market_available=event is not None,
        probabilities=probabilities,
        consensus=consensus,
        risk_profile=risk_profile,
        paper_bankroll=paper_bankroll,
        decisions=decisions,
    )


def _analyze_one_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    city: CityConfig | None = None,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
    emos_lookup: dict | None = None,
    sizing_model=_UNSET,
    pause_reasons: dict[tuple[str, str], str | None] | None = None,
) -> None:
    context = build_scan_context(
        args,
        target,
        adapter,
        calibrator,
        config,
        store,
        color,
        city=city,
        event_hint=event_hint,
        event_lookup_done=event_lookup_done,
        kalshi_client=kalshi_client,
        emos_lookup=emos_lookup,
        sizing_model=sizing_model,
        fallback_event_title="No live Kalshi event found; probability-only fallback ladder",
    )
    city = context.city
    series_ticker = context.series_ticker
    forecast = context.forecast
    intraday = context.intraday
    ensemble = context.ensemble
    event = context.event
    event_title = context.event_title
    probabilities = context.probabilities
    consensus = context.consensus
    risk_profile = context.risk_profile
    paper_bankroll = context.paper_bankroll
    decisions = context.decisions
    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper entry disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper entry disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(
                target, forecast, intraday, city=city, risk_profile=risk_profile
            )
    if args.place_paper and entry_allowed:
        pause_reason = _cached_paper_entry_pause_reason(
            store,
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
            cache=pause_reasons,
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason
    paper_trader = PaperTrader(
        store,
        config,
        risk_profile=risk_profile,
        entry_mode=args.paper_entry_mode,
        series_ticker=series_ticker,
    )
    display_decisions = paper_trader.with_paper_stakes(decisions, args.paper_stake)
    daily_budget_remaining = None
    if args.daily_budget is not None:
        daily_budget_remaining = store.remaining_daily_budget(
            target.isoformat(),
            args.daily_budget,
            risk_profile=risk_profile,
        )
        display_decisions = paper_trader.with_daily_budget(display_decisions, daily_budget_remaining)
    display_decisions = paper_trader.with_entry_mode(display_decisions)
    if not entry_allowed and entry_block_reason:
        display_decisions = _block_entry_decisions(display_decisions, entry_block_reason)

    forecast_snapshot_id = None
    market_snapshot_id = None
    if not getattr(args, "skip_context_snapshots", False):
        forecast_snapshot_id = store.record_forecast(forecast)
        if event:
            market_snapshot_id = store.record_market(event)
        store.record_probabilities(target.isoformat(), probabilities.values())
    store.record_decisions(
        target.isoformat(),
        display_decisions,
        forecast=forecast,
        intraday=intraday,
        event=event,
        market_consensus=consensus,
        risk_profile=risk_profile,
        bankroll=paper_bankroll,
        strategy_config=config,
        forecast_snapshot_id=forecast_snapshot_id,
        market_snapshot_id=market_snapshot_id,
    )

    order_ids = []
    if args.place_paper and entry_allowed:
        order_ids = paper_trader.place_approved(
            target.isoformat(),
            decisions,
            stake_dollars=args.paper_stake,
            daily_budget=daily_budget_remaining,
            bankroll=paper_bankroll,
        )

    _print_analysis(
        event_title,
        forecast,
        display_decisions,
        placed_ids=order_ids,
        market_available=event is not None,
        color=color,
        paper_stake=args.paper_stake,
        daily_budget=args.daily_budget,
        daily_budget_remaining=daily_budget_remaining,
        intraday=intraday,
        ensemble=ensemble,
        entry_block_reason=entry_block_reason,
        consensus=consensus,
    )


def _research_observed_high_state(intraday: IntradaySnapshot | None) -> str:
    if intraday is None or intraday.observed_high_f is None:
        high = "unavailable"
    else:
        high = f"{float(intraday.observed_high_f):.3f}"
    return f"complete={int(bool(intraday and intraday.is_complete))};high={high}"


def _execute_research_scan_context(
    context: ScanContext,
    *,
    target: date,
    store: PaperStore,
    config: StrategyConfig,
    entry_allowed: bool,
    entry_block_reason: str | None,
    place_paper: bool,
    forecast_snapshot_id: int | None,
    market_snapshot_id: int | None,
    scan_run_id: str | None = None,
):
    """Evaluate two books from one immutable in-memory research context."""

    objective_day = store.research_objective_day()
    lead_days = (target - objective_day).days
    lead_bucket = canonical_research_lead_bucket(lead_days)
    run_id = scan_run_id or f"research-{uuid.uuid4().hex[:16]}"
    decisions = list(context.decisions)
    if not entry_allowed and entry_block_reason:
        decisions = _block_entry_decisions(decisions, entry_block_reason)
    target_state = store.research_account_state(account_id=TARGET_POLICY.account_id)
    motion_state = store.research_account_state(account_id=MOTION_POLICY.account_id)
    target_cash = float(target_state["available_cash"]) if target_state else 0.0
    motion_cash = float(motion_state["available_cash"]) if motion_state else 0.0
    target_realized = store.research_realized_pnl_for_day(
        account_id=TARGET_POLICY.account_id,
        objective_day=objective_day,
    )
    motion_realized = store.research_realized_pnl_for_day(
        account_id=MOTION_POLICY.account_id,
        objective_day=objective_day,
    )
    opportunities = [
        ResearchOpportunity(decision, target.isoformat(), lead_days)
        for decision in decisions
    ]
    plans = allocate_research_plans(
        opportunities,
        target_available_cash=target_cash,
        motion_available_cash=motion_cash,
        realized_today=target_realized,
        motion_realized_today=motion_realized,
        run_id=run_id,
    )
    execution_config = strategy_config_for_profile("research")
    execution = PaperTrader(
        store,
        execution_config,
        risk_profile="research",
        entry_mode="limit",
        series_ticker=context.series_ticker,
    ).execute_research_plans(
        target.isoformat(),
        plans,
        source_decisions=decisions,
        objective_day=objective_day.isoformat(),
        lead_bucket=lead_bucket,
        scan_run_id=run_id,
        observed_high_state=_research_observed_high_state(context.intraday),
        forecast=context.forecast,
        intraday=context.intraday,
        event=context.event,
        market_consensus=context.consensus,
        forecast_snapshot_id=forecast_snapshot_id,
        market_snapshot_id=market_snapshot_id,
        admit_orders=place_paper and entry_allowed,
    )
    return plans, execution, decisions


def _research_portfolio_scan_from_context(
    args: argparse.Namespace,
    target: date,
    context: ScanContext,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
) -> None:
    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if context.event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper portfolio disabled: target date is not listed as a live event yet"
            )
        elif not context.event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper portfolio disabled: event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(
                target,
                context.forecast,
                context.intraday,
                city=context.city,
                risk_profile="research",
            )

    forecast_snapshot_id = None
    market_snapshot_id = None
    if not getattr(args, "skip_context_snapshots", False):
        forecast_snapshot_id = store.record_forecast(context.forecast)
        if context.event:
            market_snapshot_id = store.record_market(context.event)
        store.record_probabilities(target.isoformat(), context.probabilities.values())

    plans, execution, recorded_decisions = _execute_research_scan_context(
        context,
        target=target,
        store=store,
        config=config,
        entry_allowed=entry_allowed,
        entry_block_reason=entry_block_reason,
        place_paper=bool(args.place_paper),
        forecast_snapshot_id=forecast_snapshot_id,
        market_snapshot_id=market_snapshot_id,
    )
    placed_ids = [*execution.target_order_ids, *execution.motion_order_ids]
    _print_portfolio_scan(
        context.event_title,
        context.forecast,
        plans.target,
        recorded_decisions,
        placed_ids=placed_ids,
        market_available=context.market_available,
        color=color,
        intraday=context.intraday,
        ensemble=context.ensemble,
        entry_block_reason=entry_block_reason,
        consensus=context.consensus,
    )


def _portfolio_scan_one_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    city: CityConfig | None = None,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
    emos_lookup: dict | None = None,
    sizing_model=_UNSET,
    pause_reasons: dict[tuple[str, str], str | None] | None = None,
) -> None:
    context = build_scan_context(
        args,
        target,
        adapter,
        calibrator,
        config,
        store,
        color,
        city=city,
        event_hint=event_hint,
        event_lookup_done=event_lookup_done,
        kalshi_client=kalshi_client,
        emos_lookup=emos_lookup,
        sizing_model=sizing_model,
        fallback_event_title="No live Kalshi event found; portfolio scan is research-only",
    )
    city = context.city
    series_ticker = context.series_ticker
    forecast = context.forecast
    intraday = context.intraday
    ensemble = context.ensemble
    event = context.event
    markets = context.markets
    event_title = context.event_title
    market_available = context.market_available
    probabilities = context.probabilities
    consensus = context.consensus
    risk_profile = context.risk_profile
    paper_bankroll = context.paper_bankroll
    decisions = context.decisions
    if risk_profile == "research":
        _research_portfolio_scan_from_context(
            args,
            target,
            context,
            config,
            store,
            color,
        )
        return
    opportunities = build_arbitrage_opportunities(
        markets,
        config=config,
        bankroll=paper_bankroll,
        max_spend=args.max_arb_spend,
        min_profit=args.min_profit,
    )
    plan = allocate_portfolio(
        decisions,
        arbitrage_opportunities=opportunities,
        bankroll=paper_bankroll,
        risk_profile=risk_profile,
        bin_yes_probs={ticker: prob.probability for ticker, prob in probabilities.items()},
        joint_kelly_enabled=config.joint_kelly_enabled,
    )

    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper portfolio disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper portfolio disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(
                target, forecast, intraday, city=city, risk_profile=risk_profile
            )
    paper_trader = PaperTrader(
        store,
        config,
        risk_profile=risk_profile,
        entry_mode=args.paper_entry_mode,
        series_ticker=series_ticker,
    )
    if args.place_paper and entry_allowed:
        pause_reason = _cached_paper_entry_pause_reason(
            store,
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
            cache=pause_reasons,
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason

    decisions_to_record = _portfolio_decisions_for_recording(decisions, plan)
    if not entry_allowed and entry_block_reason:
        if risk_profile == "research":
            paper_trader.record_research_shadow_candidates(
                target.isoformat(),
                _entry_blocked_shadow_decisions(plan.decisions, entry_block_reason),
                sampled=False,
            )
        decisions_to_record = _block_entry_decisions(decisions_to_record, entry_block_reason)

    forecast_snapshot_id = None
    market_snapshot_id = None
    if not getattr(args, "skip_context_snapshots", False):
        forecast_snapshot_id = store.record_forecast(forecast)
        if event:
            market_snapshot_id = store.record_market(event)
        store.record_probabilities(target.isoformat(), probabilities.values())
    store.record_decisions(
        target.isoformat(),
        decisions_to_record,
        forecast=forecast,
        intraday=intraday,
        event=event,
        market_consensus=consensus,
        risk_profile=risk_profile,
        bankroll=paper_bankroll,
        strategy_config=config,
        forecast_snapshot_id=forecast_snapshot_id,
        market_snapshot_id=market_snapshot_id,
    )

    placed_ids: list[int] = []
    if args.place_paper and entry_allowed and plan.approved:
        placed_ids = _place_portfolio_orders(
            paper_trader,
            target.isoformat(),
            plan,
            bankroll=paper_bankroll,
            warn=lambda message: print(color.yellow(message), file=sys.stderr),
        )

    _print_portfolio_scan(
        event_title,
        forecast,
        plan,
        decisions_to_record,
        placed_ids=placed_ids,
        market_available=market_available,
        color=color,
        intraday=intraday,
        ensemble=ensemble,
        entry_block_reason=entry_block_reason,
        consensus=consensus,
    )


def _place_portfolio_orders(
    paper_trader: PaperTrader,
    target_date: str,
    plan: PortfolioPlan,
    *,
    bankroll: float,
    warn=print,
) -> list[int]:
    """Place one plan while containing an individual arbitrage-group failure."""

    placed_ids: list[int] = []
    for opportunity in plan.arbitrage_opportunities:
        try:
            placed_ids.extend(
                paper_trader.place_arbitrage(
                    target_date,
                    opportunity,
                    bankroll=bankroll,
                )
            )
        except ArbitrageContainmentError:
            raise
        except Exception as exc:
            warn(
                f"arbitrage group skipped after contained placement failure: "
                f"{type(exc).__name__}: {exc}"
            )
    directional = [
        leg.decision
        for leg in plan.legs
        if leg.sleeve != "arbitrage"
    ]
    placed_ids.extend(
        paper_trader.place_approved(
            target_date,
            directional,
            bankroll=bankroll,
        )
    )
    return placed_ids


def _arbitrage_one_target(
    args: argparse.Namespace,
    target,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    city: CityConfig | None = None,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
    pause_reasons: dict[tuple[str, str], str | None] | None = None,
) -> None:
    city = city or get_city("sfo")
    series_ticker = city.series_ticker
    event = event_hint
    if event_lookup_done:
        pass
    elif args.offline_events:
        events = load_event_snapshots(args.offline_events, target)
        event = events[0] if events else None
    else:
        try:
            client = kalshi_client or KalshiPublicClient()
            event = client.find_event_by_date(target, series_ticker=series_ticker)
        except (URLError, OSError) as exc:
            print(color.yellow(f"warning: live Kalshi lookup failed ({exc}); no active ladder available"), file=sys.stderr)
            event = None

    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
        market_available = True
    else:
        # Arbitrage needs live prices; the synthetic ladder only keeps the
        # research record shaped. Center is irrelevant to the empty book.
        markets = fallback_bins(
            f"{series_ticker}-{format_event_date_token(target)}-PAPER", 70.0
        )
        event_title = "No live Kalshi event found; arbitrage scan is research-only"
        market_available = False

    risk_profile = _risk_profile_name(args)
    paper_bankroll = _sizing_bankroll(store, config, risk_profile)
    opportunities = build_arbitrage_opportunities(
        markets,
        config=config,
        bankroll=paper_bankroll,
        max_spend=args.max_arb_spend,
        min_profit=args.min_profit,
    )

    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper arbitrage disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper arbitrage disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(
                target, None, None, city=city, risk_profile=risk_profile
            )

    paper_trader = PaperTrader(store, config, risk_profile=risk_profile)
    if args.place_paper and entry_allowed:
        pause_reason = _cached_paper_entry_pause_reason(
            store,
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
            cache=pause_reasons,
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason

    if event and not getattr(args, "skip_context_snapshots", False):
        store.record_market(event)

    placed_ids: list[int] = []
    if args.place_paper and entry_allowed:
        for opportunity in opportunities:
            if not opportunity.approved:
                continue
            placed_ids.extend(
                paper_trader.place_arbitrage(
                    target.isoformat(),
                    opportunity,
                    bankroll=paper_bankroll,
                )
            )

    _print_arbitrage(
        event_title,
        target.isoformat(),
        opportunities,
        placed_ids=placed_ids,
        market_available=market_available,
        color=color,
        max_spend=args.max_arb_spend,
        min_profit=args.min_profit,
        entry_block_reason=entry_block_reason,
    )


def _tail_basket_one_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    city: CityConfig | None = None,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
    emos_lookup: dict | None = None,
    sizing_model=_UNSET,
    pause_reasons: dict[tuple[str, str], str | None] | None = None,
) -> None:
    city = city or get_city("sfo")
    series_ticker = city.series_ticker
    forecast = adapter.latest_blend(target)
    _enforce_live_forecast_freshness(forecast, config)
    intraday = _intraday_for_target(args, target, adapter, city=city)
    observed_high_f = intraday.observed_high_f if intraday else None
    if intraday is not None and not has_forecaster_observed_high_adjustment(forecast):
        forecast = adapter.apply_intraday_update(forecast, intraday)
    # GFS-ensemble sharpening is an SFO-validated feature (2 ensemble-API
    # calls per target); at fifteen cities on a 5-minute cadence it would blow
    # the free ensemble quota, so EMOS-only cities run without it -- their
    # sigma already comes from the calibrated EMOS fit.
    ensemble = (
        _ensemble_for_target(args, target, forecast.predicted_high_f, color, city=city)
        if city.has_full_blend
        else None
    )
    event = event_hint
    if event_lookup_done:
        pass
    elif args.offline_events:
        events = load_event_snapshots(args.offline_events, target)
        event = events[0] if events else None
    else:
        try:
            client = kalshi_client or KalshiPublicClient()
            event = client.find_event_by_date(target, series_ticker=series_ticker)
        except (URLError, OSError) as exc:
            print(color.yellow(f"warning: live Kalshi lookup failed ({exc}); using probability-only ladder"), file=sys.stderr)
            event = None

    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
    else:
        markets = fallback_bins(
            f"{series_ticker}-{format_event_date_token(target)}-PAPER",
            forecast.predicted_high_f,
        )
        event_title = "No live Kalshi event found; probability-only fallback ladder"

    # lead_days=None: the live serve writes each rolling target at its TRUE lead
    # (next-day=1, 2-day-out=2), so read across leads keyed by target_date. A
    # fixed lead 1 would silently drop the 2-day-out market's EMOS distribution.
    if emos_lookup is None:
        emos_lookup = (
            adapter.load_emos_mu_sigma(lead_days=None)
            if config.emos_distribution_enabled
            else {}
        )
    probabilities = calibrator.bucket_probabilities(
        markets,
        forecast.predicted_high_f,
        source_spread_f=forecast.source_spread_f,
        observed_high_f=observed_high_f,
        ensemble=ensemble,
        intraday=intraday,
        emos_mu_sigma=emos_lookup.get(target),
        standard_timezone=intraday_timezone_for_city(city),
    )
    risk_profile = _risk_profile_name(args)
    paper_bankroll = _sizing_bankroll(store, config, risk_profile)
    evaluator = TradeEvaluator(
        config,
        sizing_model=(
            _build_sizing_model(config, store) if sizing_model is _UNSET else sizing_model
        ),
    )
    basket = build_tail_basket(
        markets,
        probabilities,
        predicted_high_f=forecast.predicted_high_f,
        evaluator=evaluator,
        bankroll=paper_bankroll,
        tail_distance_f=args.tail_distance,
        # In 'kelly' mode pass no fixed stake so each leg keeps the evaluator's
        # risk-budget (Kelly + comfort) size instead of a hardcoded few dollars.
        tail_stake=None if args.basket_sizing == "kelly" else args.tail_stake,
        center_stake=args.center_stake,
        max_tail_yes_probability=args.max_tail_probability,
        max_basket_spend=args.max_basket_spend,
        max_worst_case_loss=args.max_worst_case_loss,
        source_spread_f=forecast.source_spread_f,
    )

    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper entry disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper entry disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(
                target, forecast, intraday, city=city, risk_profile=risk_profile
            )

    if args.place_paper and entry_allowed:
        pause_reason = _cached_paper_entry_pause_reason(
            store,
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
            cache=pause_reasons,
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason
    decisions_to_record = basket.decisions_for_recording()
    if not entry_allowed and entry_block_reason:
        decisions_to_record = _block_entry_decisions(decisions_to_record, entry_block_reason)

    forecast_snapshot_id = None
    market_snapshot_id = None
    if not getattr(args, "skip_context_snapshots", False):
        forecast_snapshot_id = store.record_forecast(forecast)
        if event:
            market_snapshot_id = store.record_market(event)
        store.record_probabilities(target.isoformat(), probabilities.values())
    store.record_decisions(
        target.isoformat(),
        decisions_to_record,
        forecast=forecast,
        intraday=intraday,
        event=event,
        risk_profile=risk_profile,
        bankroll=paper_bankroll,
        strategy_config=config,
        forecast_snapshot_id=forecast_snapshot_id,
        market_snapshot_id=market_snapshot_id,
    )

    order_ids = []
    if args.place_paper and entry_allowed and basket.approved:
        paper_trader = PaperTrader(store, config, risk_profile=risk_profile)
        # A tail basket is a worst-case-bounded structure meant to be held to
        # settlement. Tag its legs as one group so the monitor never closes a
        # single leg on an intraday take-profit/stop-loss and breaks the bound.
        order_ids = paper_trader.place_approved(
            target.isoformat(),
            basket.decisions,
            daily_budget=None,
            bankroll=paper_bankroll,
            group_id=f"BASKET-{uuid.uuid4().hex[:12]}",
        )

    _print_tail_basket(
        event_title,
        forecast,
        basket,
        placed_ids=order_ids,
        market_available=event is not None,
        color=color,
        intraday=intraday,
        ensemble=ensemble,
        entry_block_reason=entry_block_reason,
    )


def _ensemble_for_target(
    args: argparse.Namespace,
    target,
    station_center_high_f: float,
    color: Color,
    city: CityConfig | None = None,
) -> EnsembleSnapshot | None:
    if args.no_ensemble:
        return None
    try:
        return SfoEnsembleClient(
            timeout=args.ensemble_timeout, city=city
        ).station_aligned_snapshot(
            target,
            station_center_high_f,
        )
    except (OpenMeteoEnsembleError, OSError, TimeoutError, URLError) as exc:
        print(
            color.yellow(f"warning: station-aligned ensemble lookup failed ({exc}); using residual calibration only"),
            file=sys.stderr,
        )
        return None


def _intraday_for_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
    city: CityConfig | None = None,
) -> IntradaySnapshot | None:
    today = settlement_today(None, city)
    if target != today:
        return None
    intraday = adapter.intraday_snapshot(target)
    if args.observed_high is None:
        return intraday
    if intraday is None:
        return IntradaySnapshot(
            target_date=target,
            observed_high_f=args.observed_high,
            latest_temp_f=None,
            latest_observed_at=None,
            remaining_forecast_high_f=None,
            forecast_fetched_at=None,
            observation_count=0,
        )
    return replace(intraday, observed_high_f=args.observed_high)


def _paper_entry_gate_for_target(
    target,
    forecast,
    intraday: IntradaySnapshot | None,
    *,
    city: CityConfig | None = None,
    now: datetime | None = None,
    risk_profile: str | None = "live",
) -> tuple[bool, str | None]:
    # A single-source forecast (Google-cache fallback when the multi-source
    # blend is unavailable) reports a 0.0 source spread, which silently passes
    # the disagreement gate and skips sigma widening. Refuse to open paper
    # positions on an uncorroborated point forecast on any target date.
    if forecast is not None and getattr(forecast, "source_count", 2) < 2:
        return False, (
            "paper entry disabled: single-source forecast (no multi-source "
            "corroboration); disagreement gate and sigma widening cannot engage"
        )
    local_now = settlement_clock(now, city)
    if target != local_now.date():
        return True, None
    if normalize_risk_profile_name(risk_profile) == "live":
        return (
            False,
            "live paper entry requires min_lead_days=1; same-day signals are research-only",
        )
    if intraday is not None and intraday.is_complete:
        return False, "same-day entry disabled: official daily high is complete; monitor/settle only"

    cutoff_hour = _same_day_entry_cutoff_hour()
    if local_now.hour >= cutoff_hour:
        return (
            False,
            (
                "same-day entry disabled: local peak/high window has passed; "
                "rolling scanner shifts to later target dates"
            ),
        )
    return True, None


def _block_entry_decisions(decisions, reason: str):
    return [
        replace(
            decision,
            approved=False,
            signal_approved=_decision_signal_approved(decision),
            entry_block_reason=reason,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=[reason, *decision.reasons],
        )
        for decision in decisions
    ]


def _entry_blocked_shadow_decisions(decisions, reason: str):
    return [
        replace(
            decision,
            approved=False,
            signal_approved=_decision_signal_approved(decision),
            entry_block_reason=reason,
            reasons=[reason, *decision.reasons],
        )
        for decision in decisions
    ]


def _portfolio_decisions_for_recording(decisions, plan: PortfolioPlan):
    selected_by_key = {
        _portfolio_decision_key(leg.decision): replace(
            leg.decision,
            signal_approved=_decision_signal_approved(leg.decision),
        )
        for leg in plan.legs
    }
    recorded = []
    seen: set[tuple[str, str]] = set()
    for decision in decisions:
        key = _portfolio_decision_key(decision)
        seen.add(key)
        selected = selected_by_key.get(key)
        if selected is not None:
            recorded.append(selected)
        elif decision.approved:
            recorded.append(
                replace(
                    decision,
                    approved=False,
                    signal_approved=True,
                    recommended_contracts=0.0,
                    expected_profit=0.0,
                    reasons=[*decision.reasons, "portfolio not allocated by shared risk budget"],
                )
            )
        else:
            recorded.append(decision)
    for leg in plan.legs:
        key = _portfolio_decision_key(leg.decision)
        if key not in seen:
            recorded.append(leg.decision)
            seen.add(key)
    return recorded


def _decision_signal_approved(decision) -> bool:
    return bool(
        decision.signal_approved if decision.signal_approved is not None else decision.approved
    )


def _portfolio_decision_key(decision) -> tuple[str, str]:
    return (str(decision.ticker), str(decision.side).upper())


def _same_day_entry_cutoff_hour() -> int:
    raw = os.getenv("PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR", str(DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR
    return min(23, max(0, value))


class AdapterFactory(Protocol):
    def __call__(
        self, forecaster_root: Path, *, city: CityConfig
    ) -> SfoForecasterAdapter: ...


class ScanTarget(Protocol):
    def __call__(
        self,
        args: argparse.Namespace,
        target: date,
        adapter: SfoForecasterAdapter,
        calibrator: ResidualCalibrator,
        config: StrategyConfig,
        store: PaperStore,
        color: Color,
        *,
        city: CityConfig,
        event_hint: EventSnapshot | None,
        event_lookup_done: bool,
        kalshi_client: KalshiPublicClient,
        emos_lookup: dict[date, tuple[float, float]],
        sizing_model: object,
        pause_reasons: dict[tuple[str, str], str | None],
    ) -> None: ...


class ArbitrageTarget(Protocol):
    def __call__(
        self,
        args: argparse.Namespace,
        target: date,
        config: StrategyConfig,
        store: PaperStore,
        color: Color,
        *,
        city: CityConfig,
        event_hint: EventSnapshot | None,
        event_lookup_done: bool,
        kalshi_client: KalshiPublicClient,
        pause_reasons: dict[tuple[str, str], str | None],
    ) -> None: ...


@dataclass(frozen=True)
class ScanCommandDependencies:
    """Patchable command wiring kept separate from the scan engine."""

    cities_for_args: Callable[[argparse.Namespace], tuple[CityConfig, ...]]
    config_for_args: Callable[[argparse.Namespace], StrategyConfig]
    resolve_targets: Callable[
        [argparse.Namespace, Color, KalshiPublicClient, CityConfig | None],
        tuple[list[date], dict[date, EventSnapshot]],
    ]
    client_factory: Callable[[], KalshiPublicClient]
    store_factory: Callable[[Path], PaperStore]
    city_config_factory: Callable[[StrategyConfig, CityConfig], StrategyConfig]
    adapter_factory: "AdapterFactory"
    calibrator_factory: Callable[
        [Iterable[ForecastOutcome], StrategyConfig], ResidualCalibrator
    ]
    sizing_model_factory: Callable[[StrategyConfig, PaperStore], object | None]
    analyze_target: "ScanTarget"
    tail_basket_target: "ScanTarget"
    arbitrage_target: "ArbitrageTarget"
    portfolio_target: "ScanTarget"
    city_lookup: Callable[[str], CityConfig]


def _command_cities_for_args(args: argparse.Namespace) -> tuple[CityConfig, ...]:
    value = getattr(args, "cities", None) or os.getenv("PAPER_CITIES", "all")
    return parse_city_slugs(value)


def _command_config_for_args(args: argparse.Namespace) -> StrategyConfig:
    base = strategy_config_for_profile(getattr(args, "risk_profile", None))
    bankroll = getattr(args, "bankroll", None)
    return base if bankroll is None else replace(base, paper_bankroll=bankroll)


def default_scan_command_dependencies() -> ScanCommandDependencies:
    return ScanCommandDependencies(
        cities_for_args=_command_cities_for_args,
        config_for_args=_command_config_for_args,
        resolve_targets=_resolve_analysis_targets,
        client_factory=KalshiPublicClient,
        store_factory=PaperStore,
        city_config_factory=config_for_city,
        adapter_factory=SfoForecasterAdapter,
        calibrator_factory=ResidualCalibrator,
        sizing_model_factory=_build_sizing_model,
        analyze_target=_analyze_one_target,
        tail_basket_target=_tail_basket_one_target,
        arbitrage_target=_arbitrage_one_target,
        portfolio_target=_portfolio_scan_one_target,
        city_lookup=get_city,
    )


def cmd_analyze(
    args: argparse.Namespace,
    *,
    dependencies: ScanCommandDependencies | None = None,
) -> int:
    dependencies = dependencies or default_scan_command_dependencies()
    color = Color.from_no_color(args.no_color)
    if args.paper_stake is not None and args.daily_budget is not None:
        raise ValueError("use either --paper-stake or --daily-budget, not both")
    base_config = dependencies.config_for_args(args)
    kalshi_client = dependencies.client_factory()
    store = dependencies.store_factory(args.db_path)
    scanned_any = False
    for city_idx, city in enumerate(dependencies.cities_for_args(args)):
        if city_idx:
            print("")
            print("#" * 92)
            print("")
        config = dependencies.city_config_factory(base_config, city)
        targets, live_events_by_target = dependencies.resolve_targets(
            args, color, kalshi_client, city
        )
        if not targets:
            print(color.yellow(f"[{city.slug}] no eligible target dates found"))
            continue
        adapter = dependencies.adapter_factory(args.forecaster_root, city=city)
        try:
            outcomes = adapter.load_calibration_outcomes(args.calibration_source)
            calibrator = dependencies.calibrator_factory(outcomes, config)
        except (ForecastDataError, ValueError) as exc:
            print(
                color.yellow(f"[{city.slug}] skipped: calibration unavailable ({exc})"),
                file=sys.stderr,
            )
            continue

        emos_lookup = (
            adapter.load_emos_mu_sigma(lead_days=None)
            if config.emos_distribution_enabled
            else {}
        )
        sizing_model = dependencies.sizing_model_factory(config, store)
        pause_reasons: dict[tuple[str, str], str | None] = {}

        for idx, target in enumerate(targets):
            if idx:
                print("")
                print("=" * 92)
                print("")
            try:
                dependencies.analyze_target(
                    args,
                    target,
                    adapter,
                    calibrator,
                    config,
                    store,
                    color,
                    city=city,
                    event_hint=live_events_by_target.get(target),
                    event_lookup_done=target in live_events_by_target,
                    kalshi_client=kalshi_client,
                    emos_lookup=emos_lookup,
                    sizing_model=sizing_model,
                    pause_reasons=pause_reasons,
                )
                scanned_any = True
            except ForecastDataError as exc:
                print(
                    color.yellow(f"[{city.slug}] {target.isoformat()}: skipped ({exc})"),
                    file=sys.stderr,
                )
    if not scanned_any:
        print(color.yellow("no city produced an analyzable target"))
    return 0


def cmd_tail_basket(
    args: argparse.Namespace,
    *,
    dependencies: ScanCommandDependencies | None = None,
) -> int:
    dependencies = dependencies or default_scan_command_dependencies()
    color = Color.from_no_color(args.no_color)
    city = dependencies.city_lookup(getattr(args, "city", None) or "sfo")
    config = dependencies.city_config_factory(dependencies.config_for_args(args), city)
    kalshi_client = dependencies.client_factory()
    targets, live_events_by_target = dependencies.resolve_targets(
        args, color, kalshi_client, city
    )
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    adapter = dependencies.adapter_factory(args.forecaster_root, city=city)
    outcomes = adapter.load_calibration_outcomes(args.calibration_source)
    calibrator = dependencies.calibrator_factory(outcomes, config)
    store = dependencies.store_factory(args.db_path)
    emos_lookup = (
        adapter.load_emos_mu_sigma(lead_days=None)
        if config.emos_distribution_enabled
        else {}
    )
    sizing_model = dependencies.sizing_model_factory(config, store)
    pause_reasons: dict[tuple[str, str], str | None] = {}

    for idx, target in enumerate(targets):
        if idx:
            print("")
            print("=" * 92)
            print("")
        dependencies.tail_basket_target(
            args,
            target,
            adapter,
            calibrator,
            config,
            store,
            color,
            city=city,
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
            emos_lookup=emos_lookup,
            sizing_model=sizing_model,
            pause_reasons=pause_reasons,
        )
    return 0


def cmd_arbitrage(
    args: argparse.Namespace,
    *,
    dependencies: ScanCommandDependencies | None = None,
) -> int:
    dependencies = dependencies or default_scan_command_dependencies()
    color = Color.from_no_color(args.no_color)
    city = dependencies.city_lookup(getattr(args, "city", None) or "sfo")
    config = dependencies.city_config_factory(dependencies.config_for_args(args), city)
    kalshi_client = dependencies.client_factory()
    targets, live_events_by_target = dependencies.resolve_targets(
        args, color, kalshi_client, city
    )
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    store = dependencies.store_factory(args.db_path)
    pause_reasons: dict[tuple[str, str], str | None] = {}

    for idx, target in enumerate(targets):
        if idx:
            print("")
            print("=" * 92)
            print("")
        dependencies.arbitrage_target(
            args,
            target,
            config,
            store,
            color,
            city=city,
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
            pause_reasons=pause_reasons,
        )
    return 0


def cmd_portfolio_scan(
    args: argparse.Namespace,
    *,
    dependencies: ScanCommandDependencies | None = None,
) -> int:
    dependencies = dependencies or default_scan_command_dependencies()
    color = Color.from_no_color(args.no_color)
    base_config = dependencies.config_for_args(args)
    kalshi_client = dependencies.client_factory()
    store = dependencies.store_factory(args.db_path)
    scanned_any = False
    fatal_containment = False
    for city_idx, city in enumerate(dependencies.cities_for_args(args)):
        if city_idx:
            print("")
            print("#" * 92)
            print("")
        config = dependencies.city_config_factory(base_config, city)
        targets, live_events_by_target = dependencies.resolve_targets(
            args, color, kalshi_client, city
        )
        if not targets:
            print(color.yellow(f"[{city.slug}] no eligible target dates found"))
            continue
        adapter = dependencies.adapter_factory(args.forecaster_root, city=city)
        try:
            outcomes = adapter.load_calibration_outcomes(args.calibration_source)
            calibrator = dependencies.calibrator_factory(outcomes, config)
        except (ForecastDataError, ValueError) as exc:
            print(
                color.yellow(f"[{city.slug}] skipped: calibration unavailable ({exc})"),
                file=sys.stderr,
            )
            continue

        emos_lookup = (
            adapter.load_emos_mu_sigma(lead_days=None)
            if config.emos_distribution_enabled
            else {}
        )
        sizing_model = dependencies.sizing_model_factory(config, store)
        pause_reasons: dict[tuple[str, str], str | None] = {}

        for idx, target in enumerate(targets):
            if idx:
                print("")
                print("=" * 92)
                print("")
            try:
                dependencies.portfolio_target(
                    args,
                    target,
                    adapter,
                    calibrator,
                    config,
                    store,
                    color,
                    city=city,
                    event_hint=live_events_by_target.get(target),
                    event_lookup_done=target in live_events_by_target,
                    kalshi_client=kalshi_client,
                    emos_lookup=emos_lookup,
                    sizing_model=sizing_model,
                    pause_reasons=pause_reasons,
                )
                scanned_any = True
            except ArbitrageContainmentError as exc:
                fatal_containment = True
                print(
                    color.yellow(f"[{city.slug}] {target.isoformat()}: skipped ({exc})"),
                    file=sys.stderr,
                )
            except ForecastDataError as exc:
                print(
                    color.yellow(f"[{city.slug}] {target.isoformat()}: skipped ({exc})"),
                    file=sys.stderr,
                )
    if not scanned_any:
        print(color.yellow("no city produced a scannable target"))
    return 1 if fatal_containment else 0
