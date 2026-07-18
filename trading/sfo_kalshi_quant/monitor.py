"""Paper-position monitoring and conservative maker-fill execution."""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError

from .account import RESEARCH_ACCOUNT_ID
from .cities import CityConfig, city_for_market_ticker
from .colors import Color
from .config import (
    config_for_city,
    intraday_timezone_for_city,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from .db import PaperStore
from .exits import (
    DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST,
    ExitSignal,
    decide_exit,
    research_no_basket_hold_reason,
)
from .fees import quadratic_fee_average_per_contract
from .forecast import (
    ForecastDataError,
    SfoForecasterAdapter,
    has_forecaster_observed_high_adjustment,
)
from .kalshi import KalshiPublicClient, KalshiUnavailable
from .maker_fills import EXECUTION_MODEL_VERSION
from .models import MarketBin
from .probability import ResidualCalibrator
from .settlement_day import settlement_clock


DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR = 14
DEFAULT_MODEL_VETO_MAX_LOSS_PCT = 60.0
DEFAULT_MODEL_VETO_BUFFER = 0.08
SAME_DAY_HEARTBEAT_OBSERVATION_MAX_AGE_MINUTES = 90.0


def _default_calibration_source() -> str:
    return os.getenv("SFO_TRADING_SIGNAL_CALIBRATION_SOURCE", "lstm")


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _same_day_entry_cutoff_hour() -> int:
    raw = os.getenv(
        "PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR", str(DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR)
    )
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR
    return min(23, max(0, value))


def _validate_monitor_args(args: argparse.Namespace) -> None:
    values = [
        args.take_profit_pct,
        args.stop_loss_pct,
        args.yes_take_profit_pct,
        args.yes_stop_loss_pct,
        args.no_take_profit_pct,
        args.no_stop_loss_pct,
        args.model_veto_max_loss_pct,
    ]
    if any(value <= 0 for value in values) or args.model_veto_buffer < 0:
        raise ValueError(
            "take-profit, stop-loss, model-veto loss percentages, and model-veto buffer must be non-negative; percentages must be greater than zero"
        )


def _monitor_market_lookup(
    client: KalshiPublicClient, tickers: list[str]
) -> dict[str, MarketBin | Exception]:
    """Resolve each unique monitor ticker once, with documented batch fallback."""

    unique = list(dict.fromkeys(str(ticker) for ticker in tickers if ticker))
    resolved: dict[str, MarketBin | Exception] = {}
    batch = getattr(client, "get_markets", None)
    if callable(batch) and unique:
        try:
            resolved.update({market.ticker: market for market in batch(unique)})
        except HTTPError as exc:
            if exc.code in (401, 403, 429) or exc.code >= 500:
                return {ticker: exc for ticker in unique}
            resolved.clear()
        except (KalshiUnavailable, URLError, OSError, TimeoutError) as exc:
            return {ticker: exc for ticker in unique}
        except (KeyError, TypeError, ValueError):
            resolved.clear()
    for ticker in unique:
        if ticker in resolved:
            continue
        try:
            resolved[ticker] = client.get_market(ticker)
        except (HTTPError, KalshiUnavailable, URLError, OSError, TimeoutError) as exc:
            resolved[ticker] = exc
    return resolved


def _monitor_thresholds_for_side(args: argparse.Namespace, side: str) -> tuple[float, float]:
    normalized = side.upper()
    if normalized == "YES":
        return float(args.yes_take_profit_pct), float(args.yes_stop_loss_pct)
    if normalized == "NO":
        return float(args.no_take_profit_pct), float(args.no_stop_loss_pct)
    return float(args.take_profit_pct), float(args.stop_loss_pct)


def _is_guaranteed_payoff_group_row(row) -> bool:
    group_id = row["group_id"] if "group_id" in row.keys() else None
    return bool(group_id and not str(group_id).startswith("DEGRADED-"))


def _settlement_first_no_min_cost_for_order(row) -> float | None:
    risk_profile = normalize_risk_profile_name(str(row["risk_profile"] or "live"))
    if risk_profile == "research":
        return DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST
    return None


def _same_day_no_basket_veto_reason(
    store: PaperStore,
    row,
    *,
    side: str,
    signal: ExitSignal,
    entry_cost: float,
    net_exit: float,
    model_side_probability: float | None,
    model_veto_buffer: float,
) -> str | None:
    """Store-aware wrapper for the engine's basket rule (audit RK-01).

    The priority ordering lives in ``exits.research_no_basket_hold_reason``: a
    catastrophic stop can never be held. This wrapper only resolves the basket
    size from the store, and skips that query entirely when the engine could
    never hold the signal.
    """

    if signal.action != "STOP_LOSS" or signal.catastrophic or side.upper() != "NO":
        return None
    risk_profile = normalize_risk_profile_name(str(row["risk_profile"] or "live"))
    if risk_profile != "research" or model_side_probability is None:
        return None
    basket_rows = store.open_no_basket_orders(str(row["target_date"]), risk_profile="research")
    distinct_markets = {str(basket_row["market_ticker"]) for basket_row in basket_rows}
    return research_no_basket_hold_reason(
        signal,
        side=side,
        risk_profile=risk_profile,
        distinct_open_no_markets=len(distinct_markets),
        entry_cost=entry_cost,
        net_exit=net_exit,
        model_side_probability=model_side_probability,
        model_veto_buffer=model_veto_buffer,
    )


def _refresh_same_day_model_reads(
    store: PaperStore,
    rows,
    *,
    forecaster_root: Path,
    log=print,
    clock=settlement_clock,
    adapter_factory=SfoForecasterAdapter,
) -> int:
    """Journal fresh model reads for open same-day positions, never entries.

    The regular scanner deliberately stops considering today's event after the
    fixed-standard 14:00 entry cutoff.  When explicitly enabled, the monitor
    keeps only the probability journal alive for positions that are already
    open, using the latest EMOS distribution and station high-so-far.  No trade
    evaluator or PaperTrader is constructed on this path.
    """

    groups: dict[tuple[str, str, str, str], tuple[CityConfig, date]] = {}
    for row in rows:
        ticker = str(row["market_ticker"])
        city = city_for_market_ticker(ticker)
        if city is None:
            continue
        try:
            target = date.fromisoformat(str(row["target_date"]))
        except ValueError:
            continue
        local_now = clock(city=city)
        if (
            target != local_now.date()
            or local_now.hour < _same_day_entry_cutoff_hour()
        ):
            continue
        event_ticker = ticker.rsplit("-", 1)[0]
        profile = normalize_risk_profile_name(str(row["risk_profile"] or "live"))
        groups[(city.slug, target.isoformat(), event_ticker, profile)] = (city, target)

    written = 0
    for (_slug, target_iso, event_ticker, profile), (city, target) in groups.items():
        try:
            event = store.latest_market_snapshot(target_iso, event_ticker=event_ticker)
            if event is None or not event.markets:
                continue
            adapter = adapter_factory(forecaster_root, city=city)
            config = replace(
                config_for_city(strategy_config_for_profile(profile), city),
                emos_distribution_enabled=True,
            )
            calibrator = ResidualCalibrator(
                adapter.load_calibration_outcomes(_default_calibration_source()),
                config,
            )
            forecast = adapter.latest_emos_snapshot(target)
            if forecast is None:
                raise ForecastDataError("same-day heartbeat has no live EMOS snapshot")
            if forecast.target_date != target or forecast.station_id != city.nws_station_id:
                raise ForecastDataError(
                    "same-day heartbeat EMOS snapshot does not match city/target"
                )
            age_hours = forecast.age_hours()
            if (
                age_hours is None
                or age_hours < -(5.0 / 60.0)
                or age_hours > config.max_forecast_age_hours
            ):
                raise ForecastDataError("same-day heartbeat EMOS snapshot is stale or undated")
            if forecast.source_count < 2:
                raise ForecastDataError("same-day heartbeat EMOS snapshot is single-source")
            emos_payload = forecast.raw.get("emos") if isinstance(forecast.raw, dict) else None
            if not isinstance(emos_payload, dict):
                raise ForecastDataError("same-day heartbeat EMOS distribution is missing")
            try:
                emos = (float(emos_payload["mu"]), float(emos_payload["sigma"]))
            except (KeyError, TypeError, ValueError):
                raise ForecastDataError("same-day heartbeat EMOS distribution is malformed")
            if not all(math.isfinite(value) for value in emos) or emos[1] <= 0:
                raise ForecastDataError("same-day heartbeat EMOS distribution is invalid")
            intraday = adapter.intraday_snapshot(target)
            if (
                intraday is None
                or intraday.target_date != target
                or intraday.observed_high_f is None
                or not _heartbeat_timestamp_is_fresh(intraday.latest_observed_at)
            ):
                raise ForecastDataError(
                    "same-day heartbeat observed high is missing, stale, or mismatched"
                )
            if not has_forecaster_observed_high_adjustment(forecast):
                forecast = adapter.apply_intraday_update(forecast, intraday)
            probabilities = calibrator.bucket_probabilities(
                event.markets,
                forecast.predicted_high_f,
                source_spread_f=forecast.source_spread_f,
                observed_high_f=intraday.observed_high_f,
                intraday=intraday,
                emos_mu_sigma=emos,
                standard_timezone=intraday_timezone_for_city(city),
            )
            store.record_probabilities(target_iso, probabilities.values())
            written += len(probabilities)
        except (ForecastDataError, ValueError, OSError) as exc:
            log(
                f"same-day model heartbeat skipped {city.slug} {target_iso}: "
                f"{type(exc).__name__}: {exc}"
            )
    return written


def _heartbeat_timestamp_is_fresh(value: str | None) -> bool:
    if not value:
        return False
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age_minutes = (datetime.now(UTC) - observed.astimezone(UTC)).total_seconds() / 60.0
    return -5.0 <= age_minutes <= SAME_DAY_HEARTBEAT_OBSERVATION_MAX_AGE_MINUTES


def run_paper_monitor(
    args: argparse.Namespace,
    *,
    client_factory=KalshiPublicClient,
    strategy_config_factory=strategy_config_for_profile,
    decide_exit_fn=decide_exit,
    refresh_model_reads=_refresh_same_day_model_reads,
) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)

    model_veto_max_loss = args.model_veto_max_loss_pct / 100.0
    _validate_monitor_args(args)

    client = client_factory()
    expired = store.expire_stale_resting_orders()
    filled = _fill_resting_orders_against_live_book(store, client, color)

    rows = store.open_paper_orders(args.limit if args.limit > 0 else None)
    if not rows:
        if not filled:
            print(color.yellow("no open paper positions"))
        return 0
    if _env_enabled("PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED"):
        refresh_model_reads(
            store,
            rows,
            forecaster_root=args.forecaster_root,
            log=lambda message: print(color.yellow(message), file=sys.stderr),
        )
    quote_rows = [row for row in rows if not _is_guaranteed_payoff_group_row(row)]
    market_lookup = _monitor_market_lookup(
        client, [str(row["market_ticker"]) for row in quote_rows]
    )
    closed = 0
    inspected = 0
    for row in rows:
        inspected += 1
        side = str(row["side"] or ("NO" if "NO" in str(row["action"]).upper() else "YES")).upper()
        group_id = row["group_id"] if "group_id" in row.keys() else None
        if _is_guaranteed_payoff_group_row(row):
            # Legs of an arbitrage box/ladder or a tail basket form a single
            # guaranteed/worst-case-bounded payoff. Closing one leg early
            # converts the structure into naked directional risk, so hold every
            # grouped leg to settlement instead of applying intraday exits.
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_GUARANTEED_LEG",
                reason=f"leg of guaranteed-payoff group {group_id}; held to settlement",
            )
            print(
                f"HOLD order {row['id']} {row['market_ticker']} {side}: "
                f"guaranteed-payoff group {group_id} (held to settlement)"
            )
            continue
        take_profit_pct, stop_loss_pct = _monitor_thresholds_for_side(args, side)
        take_profit = take_profit_pct / 100.0
        stop_loss = stop_loss_pct / 100.0
        market_or_error = market_lookup[str(row["market_ticker"])]
        if isinstance(market_or_error, HTTPError):
            exc = market_or_error
            # An expired/invalid API key (401/403) must NOT be masked as a benign
            # transient HOLD -- that would silently leave every open position
            # unmanaged. Surface it loudly by re-raising; transient 4xx (e.g. a
            # 404 on a delisted market) stay a per-order FETCH_FAILED.
            if exc.code in (401, 403):
                raise
            reason = f"market fetch failed (HTTP {exc.code})"
            store.record_monitor_snapshot(row, side=side, action="FETCH_FAILED", reason=reason)
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: {reason}")
            continue
        if isinstance(
            market_or_error, (KalshiUnavailable, URLError, OSError, TimeoutError)
        ):
            exc = market_or_error
            # Genuinely transient network failures: hold this position and move on.
            # Non-network exceptions (e.g. a programming bug) now propagate instead
            # of being swallowed into a phantom HOLD across the whole book.
            reason = f"market fetch failed ({type(exc).__name__})"
            store.record_monitor_snapshot(row, side=side, action="FETCH_FAILED", reason=reason)
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: {reason}")
            continue
        market = market_or_error

        if market.status != "active":
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_INACTIVE_MARKET",
                reason=f"market status {market.status}",
                market_status=market.status,
            )
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: market status {market.status}")
            continue

        live_bid = market.side_bid(side)
        if live_bid <= 0:
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_NO_BID",
                reason="no live bid",
                market_status=market.status,
                live_bid=live_bid,
            )
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: no live bid")
            continue

        entry_cost = float(row["cost_per_contract"])
        contracts = float(row["contracts"])
        fee_config = strategy_config_factory(str(row["risk_profile"] or "live"))
        exit_fee = quadratic_fee_average_per_contract(
            live_bid,
            contracts,
            fee_multiplier=fee_config.fee_multiplier,
            taker_rate=fee_config.taker_fee_rate,
            maker_rate=fee_config.maker_fee_rate,
            series_ticker=str(row["market_ticker"]),
        )
        net_exit = live_bid - exit_fee
        pnl_pct = (net_exit - entry_cost) / entry_cost if entry_cost > 0 else 0.0
        pnl_dollars = contracts * (net_exit - entry_cost)

        # Edge-based exit decision, shared with the dashboard mirror via exits.py.
        # Take-profit fires when the net exit reaches the model's fair value for
        # the side -- always reachable, unlike the old %-of-cost target that
        # exceeded $1 for any favorite (cost > ~0.74) and silently rode every
        # favorite to settlement. When no fresh model read exists, the legacy
        # %-of-cost target is the reachable-for-cheap-positions fallback. The
        # stop-loss is the reachable downside price floor with the NO-side model
        # veto preserved (do not sell intraday noise the model still expects to win).
        model_read = store.latest_model_probability_read(
            str(row["target_date"]), str(row["market_ticker"])
        )
        model_yes_p = None if model_read is None else model_read[1]
        model_side_p = (
            (model_yes_p if side == "YES" else 1.0 - model_yes_p)
            if model_yes_p is not None
            else None
        )
        # Persist which model snapshot backed this decision (and how old it
        # was) so stale-read incidents are auditable after the fact (RK-01).
        model_read_info = None
        if model_read is not None:
            model_read_info = {
                "model_yes_probability": model_yes_p,
                "created_at": model_read[0].isoformat(),
                "age_minutes": round(
                    (datetime.now(UTC) - model_read[0]).total_seconds() / 60.0, 3
                ),
            }
        signal = decide_exit_fn(
            side=side,
            entry_cost=entry_cost,
            net_exit=net_exit,
            stop_loss_net=entry_cost * (1.0 - stop_loss),
            model_side_probability=model_side_p,
            model_veto_buffer=args.model_veto_buffer,
            model_veto_max_loss_roi=model_veto_max_loss,
            legacy_take_profit_net=entry_cost * (1.0 + take_profit),
            stop_loss_pct=stop_loss_pct,
            settlement_first_no_min_cost=_settlement_first_no_min_cost_for_order(row),
        )

        if signal.action in (
            "HOLD",
            "HOLD_MODEL_VETO",
            "HOLD_NO_MODEL_READ",
            "HOLD_SETTLEMENT_FIRST",
        ):
            store.record_monitor_snapshot(
                row,
                side=side,
                action=signal.action,
                reason=signal.reason,
                market_status=market.status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee,
                net_exit_per_contract=net_exit,
                unrealized_pnl=pnl_dollars,
                unrealized_roi=pnl_pct,
                model_read=model_read_info,
            )
            print(
                f"HOLD order {row['id']} {row['market_ticker']} {side}: "
                f"bid={live_bid:.2f} net={net_exit:.2f} unrealized={pnl_pct * 100:.1f}% "
                f"(${pnl_dollars:.2f}); {signal.reason}"
            )
            continue

        basket_veto_reason = _same_day_no_basket_veto_reason(
            store,
            row,
            side=side,
            signal=signal,
            entry_cost=entry_cost,
            net_exit=net_exit,
            model_side_probability=model_side_p,
            model_veto_buffer=args.model_veto_buffer,
        )
        if basket_veto_reason is not None:
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_NO_BASKET_VETO",
                reason=basket_veto_reason,
                market_status=market.status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee,
                net_exit_per_contract=net_exit,
                unrealized_pnl=pnl_dollars,
                unrealized_roi=pnl_pct,
                model_read=model_read_info,
            )
            print(
                f"HOLD order {row['id']} {row['market_ticker']} {side}: "
                f"bid={live_bid:.2f} net={net_exit:.2f} unrealized={pnl_pct * 100:.1f}% "
                f"(${pnl_dollars:.2f}); {basket_veto_reason}"
            )
            continue

        reason = signal.reason
        exit_kind = signal.action  # "TAKE_PROFIT" | "STOP_LOSS"

        if args.dry_run:
            store.record_monitor_snapshot(
                row,
                side=side,
                action="WOULD_CLOSE",
                reason=reason,
                market_status=market.status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee,
                net_exit_per_contract=net_exit,
                unrealized_pnl=pnl_dollars,
                unrealized_roi=pnl_pct,
                model_read=model_read_info,
            )
            print(
                f"WOULD_CLOSE order {row['id']} {row['market_ticker']} {side}: "
                f"bid={live_bid:.2f} net={net_exit:.2f} unrealized={pnl_pct * 100:.1f}% "
                f"(${pnl_dollars:.2f}); {reason}"
            )
            continue

        # An exit may only book the quantity the displayed top-bid liquidity
        # supports (audit EX-02). With no displayed size the close waits.
        bid_size = market.side_bid_size(side)
        if bid_size <= 0:
            no_depth_reason = (
                f"{reason}; close deferred: displayed {side} bid size is zero"
            )
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_NO_DISPLAYED_DEPTH",
                reason=no_depth_reason,
                market_status=market.status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee,
                net_exit_per_contract=net_exit,
                unrealized_pnl=pnl_dollars,
                unrealized_roi=pnl_pct,
                model_read=model_read_info,
            )
            print(
                f"HOLD order {row['id']} {row['market_ticker']} {side}: {no_depth_reason}"
            )
            continue

        action = "CLOSE_TAKE_PROFIT" if exit_kind == "TAKE_PROFIT" else "CLOSE_STOP_LOSS"
        store.record_monitor_snapshot(
            row,
            side=side,
            action=action,
            reason=reason,
            market_status=market.status,
            live_bid=live_bid,
            exit_fee_per_contract=exit_fee,
            net_exit_per_contract=net_exit,
            unrealized_pnl=pnl_dollars,
            unrealized_roi=pnl_pct,
            model_read=model_read_info,
        )
        try:
            closed_row = store.close_paper_order(
                int(row["id"]),
                live_bid,
                max_quantity=bid_size,
                liquidity_evidence={
                    "bid": live_bid,
                    "displayed_bid_size": bid_size,
                    "market_status": market.status,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "source": "monitor_market_lookup",
                },
            )
        except (ValueError, RuntimeError) as exc:
            # A concurrent settle/close can win the race for this row. Log and
            # keep inspecting the rest of the book instead of aborting the run.
            print(
                color.yellow(
                    f"skip order {row['id']} {row['market_ticker']} {side}: "
                    f"close failed ({type(exc).__name__}: {exc})"
                ),
                file=sys.stderr,
            )
            continue
        closed += 1
        executed = float(closed_row["contracts"])
        remaining = max(0.0, contracts - executed)
        pnl = f"${closed_row['realized_pnl']:.2f}"
        pnl = color.green(pnl) if closed_row["realized_pnl"] >= 0 else color.red(pnl)
        partial_note = (
            f" (partial: {executed:g} of {contracts:g} at displayed size "
            f"{bid_size:g}, {remaining:g} remain open)"
            if remaining > 1e-9
            else ""
        )
        print(
            f"{color.green('closed')} order {closed_row['id']} {row['market_ticker']} {side}: "
            f"bid={live_bid:.2f}; exit_fee={closed_row['exit_fee_per_contract']:.2f}; "
            f"realized_pnl={pnl}{partial_note}; {reason}"
        )

    print(
        color.cyan(
            f"paper monitor inspected {inspected}, closed {closed}, "
            f"filled {filled} resting limits, expired {expired} stale limits"
        )
    )
    return 0


def _all_public_trades_for_ticker(
    client: KalshiPublicClient,
    *,
    ticker: str,
    min_ts: int,
    max_ts: int,
) -> list[dict[str, object]]:
    """Exhaust the official cursor chain without exposing partial evidence."""

    iterator = getattr(type(client), "iter_trades", None)
    if callable(iterator):
        return list(
            client.iter_trades(
                ticker=ticker,
                min_ts=min_ts,
                max_ts=max_ts,
                limit=1000,
            )
        )

    # Compatibility path for injected/ad-hoc clients that only implement the
    # long-standing get_trades method. It follows the same cursor contract.
    trades: list[dict[str, object]] = []
    seen_trade_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        payload = client.get_trades(
            ticker=ticker,
            min_ts=min_ts,
            max_ts=max_ts,
            limit=1000,
            cursor=cursor,
        )
        for trade in payload.get("trades", []):
            if not isinstance(trade, dict):
                continue
            trade_id = str(trade.get("trade_id") or "")
            if trade_id and trade_id in seen_trade_ids:
                continue
            if trade_id:
                seen_trade_ids.add(trade_id)
            trades.append(trade)
        next_cursor = str(payload.get("cursor") or "")
        if not next_cursor:
            return trades
        if next_cursor in seen_cursors:
            raise KalshiUnavailable("trade pagination returned a repeated cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _fill_resting_orders_against_live_book(
    store: PaperStore, client: KalshiPublicClient, color: Color
) -> int:
    """Conservative maker fill via the shared single-aggressor allocator.

    Each public trade is normalized to exactly one maker side (a bid-side
    taker fills resting NO bids, an ask-side taker fills resting YES bids) and
    its volume is allocated once across compatible resting orders in
    price-time priority, after each order's estimated queue ahead (EX-01).
    Conservation holds ACROSS passes, not just within one: every capital
    fill persists per-trade volume claims, and later passes subtract those
    claims from the available volume before allocating to still-resting
    orders, so a restart or a later pass can never re-credit consumed volume.
    """

    filled = 0
    by_ticker: dict[str, list] = {}
    for row in store.resting_paper_orders():
        by_ticker.setdefault(str(row["market_ticker"]), []).append(row)
    max_ts = int(datetime.now(UTC).timestamp())
    for ticker, rows in by_ticker.items():
        created_times = []
        for row in rows:
            created_at = datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            )
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            created_times.append(created_at)
        try:
            trade_payloads = _all_public_trades_for_ticker(
                client,
                ticker=ticker,
                min_ts=int(min(created_times).timestamp()),
                max_ts=max_ts,
            )
        except (HTTPError, KalshiUnavailable, URLError, OSError, TimeoutError):
            continue
        updates = store.apply_maker_trade_batch(ticker, trade_payloads)
        for update in updates:
            updated = store.paper_order(int(update["order_id"]))
            if updated is None:
                continue
            status = str(update["status"])
            became_filled = (
                status == "PAPER_FILLED"
                and str(update["previous_status"]) != "PAPER_FILLED"
            )
            filled += int(became_filled)
            action = (
                "LIMIT_FILLED"
                if became_filled
                else (
                    "LIMIT_PARTIALLY_FILLED"
                    if float(update["filled_quantity"]) > 0
                    else "LIMIT_QUEUE_ADVANCED"
                )
            )
            store.record_monitor_snapshot(
                updated,
                side=str(updated["side"] or "YES").upper(),
                action=action,
                reason=(
                    f"{EXECUTION_MODEL_VERSION} consumed "
                    f"{float(update['queue_consumed']):.2f} "
                    f"queue and filled {float(update['filled_quantity']):.2f}; "
                    f"{float(update['remaining_quantity']):.2f} remains"
                ),
                market_status="active",
            )
            action = (
                "filled resting order"
                if updated["status"] == "PAPER_FILLED"
                else "advanced resting order"
            )
            print(
                f"{action} {updated['id']} {ticker} "
                f"{updated['side']}: {float(update['filled_quantity']):.2f} filled, "
                f"{float(update['queue_consumed']):.2f} queue consumed, "
                f"{float(update['remaining_quantity']):.2f} remaining"
            )
    return filled
