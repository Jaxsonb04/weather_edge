"""Paper-account commands behind the stable CLI facade."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

from ..cities import CITIES, CityConfig, get_city, parse_city_slugs
from ..colors import Color
from ..config import (
    DEFAULT_FORECASTER_ROOT,
    StrategyConfig,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from ..db import PaperStore
from ..forecast import SfoForecasterAdapter, parse_target_date
from ..kalshi import KalshiPublicClient
from ..models import target_date_from_event_ticker
from ..report import build_daily_report, write_report
from ..settlement_day import settlement_clock, settlement_today
from ..store.market_day_settlements import TRUTH_SOURCE_SETTLEMENT_PATH
from ..summary import (
    build_paper_summary,
    write_paper_summary,
    write_paper_summary_csv,
)
from .format import _color_edge, _color_status, _format_pnl


def _config(args: argparse.Namespace) -> StrategyConfig:
    base = strategy_config_for_profile(getattr(args, "risk_profile", None))
    if args.bankroll is None:
        return base
    return replace(base, paper_bankroll=args.bankroll)


def _cities_for_args(args: argparse.Namespace) -> tuple[CityConfig, ...]:
    value = getattr(args, "cities", None) or os.getenv("PAPER_CITIES", "all")
    return parse_city_slugs(value)


def cmd_paper_summary(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    if args.days < 1:
        raise ValueError("--days must be at least 1")
    config = _config(args)
    payload = build_paper_summary(
        db_path=args.db_path,
        forecaster_root=args.forecaster_root,
        config=config,
        days=args.days,
    )
    if args.output:
        write_paper_summary(args.output, payload)
    if args.csv:
        write_paper_summary_csv(args.csv, payload)

    totals = payload["totals"]
    print(color.cyan(color.bold(f"paper summary: {payload['window_start']} to {payload['window_end']}")))
    print(
        f"opened={totals['trades_opened']} closed={totals['trades_closed']} "
        f"settled={totals['trades_settled']} open_now={totals['open_positions']} "
        f"open_risk=${totals['open_risk']:.2f}"
    )
    realized = f"${totals['realized_pnl']:.2f}"
    realized = color.green(realized) if totals["realized_pnl"] >= 0 else color.red(realized)
    hit_rate = "-" if totals["hit_rate"] is None else f"{totals['hit_rate']:.3f}"
    roi = "-" if totals["roi"] is None else f"{totals['roi']:.3f}"
    print(
        f"window_realized={realized} cumulative=${totals['cumulative_realized_pnl']:.2f} "
        f"hit_rate={hit_rate} roi={roi}"
    )
    if totals["mean_abs_forecast_error_f"] is not None:
        print(f"mean_abs_forecast_error={totals['mean_abs_forecast_error_f']:.2f}F")
    print("")
    print(color.gray("date        opened closed settled wins losses realized cumulative hit  fc_err"))
    print(color.gray("-" * 84))
    for day in payload["days"]:
        hit = "-" if day["hit_rate"] is None else f"{day['hit_rate']:.2f}"
        err = "-" if day["forecast_error_f"] is None else f"{day['forecast_error_f']:.1f}F"
        print(
            f"{day['date']}  {day['opened']:5d} {day['closed']:6d} {day['settled']:7d} "
            f"{day['wins']:4d} {day['losses']:6d} {day['realized_pnl']:8.2f} "
            f"{day['cumulative_realized']:10.2f} {hit:>4s} {err:>6s}"
        )
    if payload["biggest_winners"]:
        print("")
        print(color.green("biggest winners:"))
        for row in payload["biggest_winners"]:
            print(f"  #{row['id']} {row['target_date']} {row['ticker']} {row['side']} ${row['realized_pnl']:+.2f}")
    if payload["biggest_losers"]:
        print("")
        print(color.red("biggest losers:"))
        for row in payload["biggest_losers"]:
            print(f"  #{row['id']} {row['target_date']} {row['ticker']} {row['side']} ${row['realized_pnl']:+.2f}")
    print("")
    print(color.cyan("learnings:"))
    for note in payload["learnings"]:
        print(f"  - {note}")
    print(color.cyan("recommended next changes:"))
    for note in payload["recommended_changes"]:
        print(f"  - {note}")
    return 0


def cmd_paper_report(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    rows = store.paper_orders(args.limit, since=args.since, until=args.until)
    if not rows:
        print(color.yellow("no paper orders recorded"))
        return 0
    for row in rows:
        status = _color_status(color, row["status"])
        pnl = _format_pnl(row["realized_pnl"])
        if row["realized_pnl"] is not None:
            pnl = color.green(pnl) if float(row["realized_pnl"]) >= 0 else color.red(pnl)
        entry_price = row["entry_price"] if row["entry_price"] is not None else row["yes_ask"]
        side = row["side"] if row["side"] else ("NO" if "NO" in str(row["action"]).upper() else "YES")
        print(
            f"id={row['id']} {row['created_at']} {row['target_date']} {row['market_ticker']} "
            f"{side} {row['contracts']:.4f} @ {float(entry_price):.2f} "
            f"spent=${float(row['contracts']) * float(row['cost_per_contract']):.2f} "
            f"edge={_color_edge(color, row['edge'])} "
            f"q={float(row['trade_quality_score']):4.1f} status={status} "
            f"exit={row['exit_price'] if row['exit_price'] is not None else '-'} "
            f"settle={row['settlement_high_f'] if row['settlement_high_f'] is not None else '-'} "
            f"pnl={pnl}"
        )
    return 0


def cmd_paper_buy(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    if args.amount <= 0:
        raise ValueError("amount must be positive")
    if args.force_fill and args.price is None:
        raise ValueError("--force-fill requires --price")
    side = args.side.upper()

    client = KalshiPublicClient()
    market = client.get_market(args.ticker)
    target = target_date_from_event_ticker(market.event_ticker)
    if target is None:
        raise ValueError(f"could not infer target date from {market.event_ticker}")

    if args.force_fill:
        entry_price = float(args.price)
        price_note = color.yellow("manual forced paper price; not a realistic fill")
        action = f"BUY_{side}_FORCE_PAPER"
        reason = "manual force-filled paper buy"
    else:
        if market.status != "active":
            raise ValueError(f"market {market.ticker} is {market.status}; cannot buy at a live ask")
        live_ask = market.side_ask(side)
        if live_ask <= 0 or live_ask >= 1:
            raise ValueError(f"market {market.ticker} has no live {side} ask to buy")
        if args.price is not None and live_ask > args.price:
            print(
                color.yellow(
                    f"limit not filled: live {side} ask is {live_ask:.2f}, "
                    f"above your limit price {args.price:.2f}"
                )
            )
            return 0
        entry_price = live_ask
        if args.price is None:
            price_note = f"live Kalshi {side} ask {live_ask:.2f}"
            action = f"BUY_{side}_LIVE_ASK_PAPER"
            reason = "manual paper buy at live ask"
        else:
            price_note = f"live Kalshi {side} ask {live_ask:.2f}, within limit {args.price:.2f}"
            action = f"BUY_{side}_LIMIT_PAPER"
            reason = "manual paper buy at live ask within limit"

    from ..fees import quadratic_fee_per_contract

    fee = quadratic_fee_per_contract(entry_price)
    cost = entry_price + fee
    desired_contracts = args.amount / cost
    filled_contracts = desired_contracts
    amount_used = args.amount
    size_note = ""
    ask_size = market.side_ask_size(side)
    if not args.force_fill and ask_size > 0 and desired_contracts > ask_size:
        filled_contracts = ask_size
        amount_used = filled_contracts * cost
        size_note = f"; capped by top {side} ask size {ask_size:.4f}"

    store = PaperStore(args.db_path)
    order_id = store.record_manual_buy(
        target_date=target.isoformat(),
        market_ticker=market.ticker,
        label=market.yes_sub_title,
        amount=amount_used,
        entry_price=entry_price,
        side=side,
        action=action,
        reason=reason,
        strike_type=market.strike_type,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
    )
    # Report the stored order, not the pre-rounding estimate: the DB rounds
    # down to whole contracts and averages the fee across them, so the
    # fractional CLI numbers can disagree with what actually got booked.
    order = store.paper_order(order_id)
    stored_contracts = float(order["contracts"])
    stored_fee = float(order["fee_per_contract"])
    stored_cost = float(order["cost_per_contract"])
    amount_at_risk = stored_contracts * stored_cost
    max_profit = stored_contracts * (1.0 - stored_cost)
    print(color.green(f"paper bought order id={order_id}"))
    print(f"ticker: {market.ticker} ({market.yes_sub_title})")
    print(f"paper amount at risk: ${amount_at_risk:.2f}{size_note}")
    print(f"entry: {price_note}")
    print(f"entry fee per contract: ${stored_fee:.2f}")
    print(f"all-in cost per contract: ${stored_cost:.2f}")
    print(f"contracts: {stored_contracts:.0f}")
    print(f"max profit if {side} wins: ${max_profit:.2f}")
    print(f"max loss if {side} loses: ${amount_at_risk:.2f}")
    return 0


def cmd_paper_close(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    open_order = store.open_paper_order(args.order_id)
    if open_order is None:
        raise ValueError(f"no open paper order found with id {args.order_id}")
    side = str(open_order["side"] or ("NO" if "NO" in str(open_order["action"]).upper() else "YES")).upper()

    if args.exit_price is None:
        market = KalshiPublicClient().get_market(open_order["market_ticker"])
        if market.status != "active":
            raise ValueError(f"market {market.ticker} is {market.status}; cannot use a live bid to close")
        live_bid = market.side_bid(side)
        if live_bid <= 0:
            raise ValueError(f"market {market.ticker} has no live {side} bid to sell into")
        exit_price = live_bid
        displayed_depth = market.side_bid_size(side)
        if displayed_depth <= 0:
            raise ValueError(
                f"market {market.ticker} has no displayed {side} bid depth to close"
            )
        price_note = f"live Kalshi {side} bid for {market.ticker}"
        max_quantity = displayed_depth
        liquidity_evidence = {
            "displayed_bid_size": displayed_depth,
            "source": "paper_close_live_market_lookup",
            "observed_at": datetime.now(UTC).isoformat(),
            "market_status": market.status,
        }
    else:
        exit_price = args.exit_price
        price_note = "manual offline override"
        max_quantity = None
        liquidity_evidence = {
            "source": "manual_offline_override",
            "observed_at": datetime.now(UTC).isoformat(),
        }

    row = store.close_paper_order(
        args.order_id,
        exit_price,
        max_quantity=max_quantity,
        liquidity_evidence=liquidity_evidence,
    )
    pnl = f"${row['realized_pnl']:.2f}"
    pnl = color.green(pnl) if row["realized_pnl"] >= 0 else color.red(pnl)
    print(
        f"{color.green('closed')} paper order {row['id']} at {row['exit_price']:.2f} using {price_note}; "
        f"exit_fee={row['exit_fee_per_contract']:.2f}; "
        f"realized_pnl={pnl}"
    )
    return 0


def cmd_paper_settle(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    target = parse_target_date(args.target_date)
    city = get_city(getattr(args, "city", None) or "sfo")
    store = PaperStore(args.db_path)
    count = store.settle_paper_orders(
        target.isoformat(), args.settlement_high, series_ticker=city.series_ticker
    )
    print(
        color.cyan(
            f"[{city.slug}] settled {count} paper orders for {target.isoformat()} "
            f"at {args.settlement_high:.0f}F"
        )
    )
    return 0


def cmd_paper_resettle(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    if args.days <= 0:
        raise ValueError("--days must be at least 1")
    adapter = SfoForecasterAdapter(args.forecaster_root)
    settlements = adapter.load_cli_settlement_truth()
    intervals = {}
    for city in CITIES:
        city_today = settlement_today(city=city)
        intervals[city.series_ticker] = (
            (city_today - timedelta(days=args.days - 1)).isoformat(),
            city_today.isoformat(),
        )
    result = PaperStore(args.db_path).verify_paper_settlements(
        settlements,
        intervals=intervals,
    )
    for row in result["checked"]:
        if row["verification_status"] == "MATCH":
            continue
        if row["verification_status"] == "MISSING_FINAL":
            detail = (
                "MISSING_FINAL "
                f"order={row['order_id']} market={row['market_ticker']} "
                f"target={row['target_date']} booked={row['booked_high_f']:.0f}F"
            )
        else:
            detail = (
                "MISMATCH "
                f"order={row['order_id']} market={row['market_ticker']} "
                f"target={row['target_date']} booked={row['booked_high_f']:.0f}F "
                f"final={row['final_high_f']:.0f}F"
            )
        print(color.yellow(detail))
    print(
        color.cyan(
            "paper settlement verification: "
            f"checked={len(result['checked'])} mismatches={result['mismatches']} "
            f"missing_final_truth={result['missing_truth']} "
            "(booked P&L unchanged)"
        )
    )
    return 0


def cmd_paper_prune(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    # These batching knobs carry argparse defaults, so the CLI path is
    # unchanged. This entry point is also called programmatically with a
    # hand-built Namespace, so fall back to the same defaults the store
    # declares rather than requiring every caller to know them.
    result = store.prune_decision_snapshots(
        full_days=args.full_days,
        dedup_days=args.dedup_days,
        batch_limit=getattr(args, "batch_limit", 5_000),
        max_batch_seconds=getattr(args, "max_batch_seconds", 2.0),
        batch_pause_seconds=getattr(args, "batch_pause_seconds", 0.15),
    )
    print(
        color.cyan(
            f"pruned decision snapshots: {result['deduped']} deduped "
            f"(kept last per market-side-day), {result['dropped']} dropped "
            f"beyond {args.dedup_days}d; {result['contexts_dropped']} contexts dropped; "
            f"{result['probabilities_dropped']} probabilities, "
            f"{result['monitor_snapshots_dropped']} monitor snapshots, "
            f"{result['forecast_snapshots_dropped']} orphan forecasts, and "
            f"{result['market_snapshots_dropped']} orphan markets dropped beyond "
            f"{args.dedup_days}d; approved rows untouched"
        )
    )
    return 0


def cmd_paper_check_foreign_keys(args: argparse.Namespace) -> int:
    violations = PaperStore(args.db_path).foreign_key_violations(limit=args.limit)
    if not violations:
        print("foreign key audit ok")
        return 0
    print(
        f"FOREIGN KEY AUDIT FAILED: showing {len(violations)} violation(s) "
        f"(limit={args.limit})",
        file=sys.stderr,
    )
    for violation in violations:
        print(
            f"{violation['table']} rowid={violation['rowid']} -> "
            f"{violation['parent']} (fk={violation['foreign_key_id']})",
            file=sys.stderr,
        )
    return 1


# The record-only pass exists for one narrow residual: a target date whose
# every lot exited early, so `settle_paper_orders` is never called for it and
# nothing ever records what the market did. That residual is produced one day at
# a time, and this pass runs unattended inside the thirty-minute settle timer
# with no dry-run. Bounding it to a recent window keeps it a residual sweep
# rather than an unbounded history backfill on a timer: unbounded, its first run
# after deploy takes and releases the write lock once per completed (series,
# date) pair, and production currently holds 308 completed pairs (319 traded in
# total, 2026-06-10 to 2026-08-17) across fourteen series. Deep history belongs
# to the explicit operator command `paper-backfill-market-day-settlements`,
# which has the --dry-run this unattended path structurally cannot offer.
#
# That division of labour is only honest because the operator command can
# actually reach deep history. It could not until the final CLI maximum became
# its third truth source: a wholly-exited series-day has no settled sibling by
# construction, and the exchange dataset covers 3 of the 154 wholly-exited
# market-days in this book. With the CLI source the same command reaches 140 of
# the 154; the 14 it does not are days whose CLI has not finalized yet, and the
# pass below picks those up in the window as soon as it does.
#
# Seven days. A target date only becomes recordable at 06:00 local the day after
# it completes, so one day covers the steady state and the remaining six are
# slack for a late-finalized CLI high or a settle-timer outage; the longest unit
# outage observed on this host was five days. Measured against the same
# production book, the window turns that 308-pair first run into 42 pairs inside
# the window, 31 of them completed and therefore actually swept, and leaves a
# steady state of roughly one date per city per day. A market-day whose CLI high
# finalizes after the window closes is not lost -- it stays in the residual and
# the operator backfill reaches it from the settled sibling, the final CLI
# maximum, or the finalized exchange result.
RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS = 7


def _recent_target_dates(
    target_dates: list[str],
    *,
    now: datetime | None = None,
    city: CityConfig | None = None,
    lookback_days: int = RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS,
) -> list[str]:
    """Target dates inside the record-only residual window."""

    clock = settlement_clock(now, city)
    earliest = clock.date() - timedelta(days=lookback_days)
    recent = []
    for target_date in target_dates:
        try:
            target = parse_target_date(target_date)
        except ValueError:
            continue
        if target >= earliest:
            recent.append(target_date)
    return recent


def cmd_paper_auto_settle(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    cities = _cities_for_args(args)
    any_open = False
    db_settled = 0
    verification_truth: dict[tuple[str, str], float] = {}
    settled_intervals: dict[str, tuple[str, str]] = {}
    outcomes_recorded = 0
    for city in cities:
        open_targets = _completed_open_target_dates(
            store.open_paper_target_dates(series_ticker=city.series_ticker),
            city=city,
        )
        # Observability residual: a target date whose every lot exited early
        # never reaches settle_paper_orders at all, so the market's own outcome
        # for it was never recorded anywhere. Collect those dates too and give
        # them a record-only pass -- it writes market_day_settlements and
        # touches no order, ledger, gate, or policy. Bounded to the recent
        # window (see RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS): this is an unattended
        # timer, not a history backfill.
        unrecorded_targets = _recent_target_dates(
            _completed_open_target_dates(
                store.unrecorded_traded_target_dates(series_ticker=city.series_ticker),
                city=city,
            ),
            city=city,
        )
        record_only_targets = [
            target for target in unrecorded_targets if target not in set(open_targets)
        ]
        if not open_targets and not record_only_targets:
            continue
        any_open = any_open or bool(open_targets)
        # Primary truth: weather.db rows explicitly classified final. This
        # prevents an older preliminary product version fetched from the live
        # endpoint from shadowing a corrected final already archived.
        adapter = SfoForecasterAdapter(args.forecaster_root, city=city)
        settlements = {
            target.isoformat(): high
            for target, high in adapter.load_cli_settlement_highs().items()
        }
        for target_date in record_only_targets:
            if target_date not in settlements:
                continue
            try:
                summary = store.record_market_day_settlements(
                    target_date,
                    settlements[target_date],
                    series_ticker=city.series_ticker,
                    truth_source=TRUTH_SOURCE_SETTLEMENT_PATH,
                )
            except Exception as exc:  # observability must never block settling
                # This pass runs before the settle loop below. An exception here
                # used to abort auto-settle for every remaining city and date --
                # a measurement-only write deciding whether real settlement runs.
                print(
                    color.red(
                        "market-day settlement recording failed for "
                        f"[{city.slug}] {target_date}: {exc} (settlement "
                        "continues; re-run paper-backfill-market-day-settlements)"
                    ),
                    file=sys.stderr,
                )
                continue
            outcomes_recorded += summary["market_days_recorded"]
        for target_date in open_targets:
            if target_date not in settlements:
                continue
            count = store.settle_paper_orders(
                target_date,
                settlements[target_date],
                series_ticker=city.series_ticker,
            )
            db_settled += count
            if count:
                print(
                    color.cyan(
                        f"[{city.slug}] settled {count} paper orders for {target_date} "
                        "from archived CLI truth (final)"
                    )
                )
                verification_truth[(city.series_ticker, target_date)] = settlements[
                    target_date
                ]
                lower, upper = settled_intervals.get(
                    city.series_ticker, (target_date, target_date)
                )
                settled_intervals[city.series_ticker] = (
                    min(lower, target_date),
                    max(upper, target_date),
                )

    if outcomes_recorded:
        print(
            color.cyan(
                f"recorded {outcomes_recorded} fully-exited market-day settlement "
                "outcome(s) that no open position would have captured"
            )
        )
    if not any_open:
        # Saying "skipped" after a record-only pass just wrote rows misreports
        # the run; the outcomes above were recorded either way.
        print(
            color.yellow(
                "auto-settle: no completed open paper target dates; the "
                "record-only outcomes above were still recorded"
                if outcomes_recorded
                else "auto-settle skipped: no completed open paper target dates"
            )
        )
        return 0
    total = db_settled
    if total:
        print(color.cyan(f"auto-settled {total} paper orders across cities"))
        # Audit ST-01: every settlement is immediately re-verified read-only
        # against the same final truth and persisted idempotently (one row per
        # settled order). Verification never edits P&L; a mismatch is an
        # incident signal, so it goes loudly to stderr.
        verification = store.verify_paper_settlements(
            verification_truth, intervals=settled_intervals
        )
        for row in verification["checked"]:
            if row["verification_status"] == "MATCH":
                continue
            print(
                color.red(
                    f"SETTLEMENT VERIFICATION {row['verification_status']}: "
                    f"order={row['order_id']} market={row['market_ticker']} "
                    f"target={row['target_date']} booked={row['booked_high_f']} "
                    f"final={row['final_high_f']} (booked P&L unchanged; open an "
                    "incident/restatement instead of editing the journal)"
                ),
                file=sys.stderr,
            )
        print(
            color.cyan(
                "settlement verification: "
                f"checked={len(verification['checked'])} "
                f"mismatches={verification['mismatches']} "
                f"missing_final_truth={verification['missing_truth']}"
            )
        )
    else:
        print(color.yellow("auto-settle: completed open targets remain but no CLI truth is available yet"))
    return 0


def cmd_paper_backfill_market_day_settlements(args: argparse.Namespace) -> int:
    """Reconstruct historical market-day outcomes from validated truth only.

    Three sources are accepted, all independently verified: the
    ``settlement_high_f`` persisted on a settled order for the same
    ``(series, target_date)``, the forecaster archive's final NWS CLI maximum
    for that station-day, and the exchange's own finalized result in
    ``dataset_kalshi_markets``. Days no source covers are unrecoverable and are
    reported, never guessed. See ``store/market_day_settlements.py`` for the
    authority ladder and for the sources that were tested and rejected.

    The CLI archive is what makes this command able to reach deep history at
    all. A series-day whose every lot exited early has no settled sibling by
    construction -- and those are exactly the market-days this table exists to
    make visible -- so without it the only route to a wholly-exited day was the
    exchange dataset, whose finalized rows stop before this book's first order.
    """

    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    # weather.db truth, all fifteen stations at once. The adapter's city only
    # selects a station for the per-city readers; this one is station-agnostic
    # and returns every (series, target_date) it holds.
    cli_settlement_highs = SfoForecasterAdapter(
        args.forecaster_root
    ).load_cli_settlement_truth()
    summary = store.backfill_market_day_settlements(
        dry_run=args.dry_run, cli_settlement_highs=cli_settlement_highs
    )
    unrecoverable = summary["unrecoverable"]
    prefix = "would record" if summary["dry_run"] else "recorded"
    print(
        color.cyan(
            f"market-day settlement backfill: {summary['traded_market_days']} traded "
            f"market-day(s); {summary['already_recorded']} already recorded; "
            f"{prefix} {summary['recorded_from_settled_sibling']} from settled "
            f"siblings, {summary['recorded_from_cli_settlements']} from final CLI "
            f"maxima, and {summary['recorded_from_dataset_markets']} from finalized "
            f"exchange results; {len(unrecoverable)} unrecoverable "
            f"({summary['cli_settlement_days_available']} CLI station-day(s) available)"
        )
    )
    for entry in unrecoverable[: args.show_unrecoverable]:
        print(
            color.yellow(
                f"unrecoverable: {entry['market_ticker']} {entry['target_date']} "
                f"({entry['reason']})"
            )
        )
    if len(unrecoverable) > args.show_unrecoverable:
        print(
            color.yellow(
                f"... {len(unrecoverable) - args.show_unrecoverable} more unrecoverable "
                "market-day(s) not shown"
            )
        )
    return 0


def _completed_open_target_dates(
    target_dates: list[str],
    *,
    now: datetime | None = None,
    city: CityConfig | None = None,
) -> list[str]:
    clock = settlement_clock(now, city)
    completed = []
    for target_date in target_dates:
        try:
            target = parse_target_date(target_date)
        except ValueError:
            continue
        grace_day = target + timedelta(days=1)
        if clock.date() > grace_day or (
            clock.date() == grace_day and clock.time() >= time(6, 0)
        ):
            completed.append(target_date)
    return completed


def cmd_paper_archive(args: argparse.Namespace) -> int:
    from ..archive import (
        archive_pending,
        cleanup_local,
        gate_missing_days,
        upload_pending,
    )

    archive_dir = args.archive_dir or (args.db_path.parent / "archive")
    if not (args.check_gate or args.upload or args.cleanup):
        exported = archive_pending(
            args.db_path,
            archive_dir,
            merge_dbs=args.merge_db,
            include_full=not args.skip_full,
        )
        print(f"archive: {exported} new file(s) under {archive_dir}")
        return 0
    if args.upload:
        upload_pending(archive_dir)
    if args.cleanup:
        cleanup_local(archive_dir, keep_days=args.keep_days)
    if args.check_gate:
        missing = gate_missing_days(args.db_path, archive_dir)
        if missing:
            preview = ", ".join(f"{t} {d}" for t, d in missing[:5])
            print(
                f"PRUNE GATE REFUSED: {len(missing)} unarchived complete day(s): {preview}",
                file=sys.stderr,
            )
            return 1
        print("prune gate ok: every complete UTC day is archived+verified")
    return 0


def cmd_paper_features(args: argparse.Namespace) -> int:
    from ..archive import build_features

    archive_dir = args.archive_dir or (args.db_path.parent / "archive")
    features_db = args.features_db or (archive_dir / "features.db")
    weather_db = args.weather_db
    if weather_db is None:
        candidate = DEFAULT_FORECASTER_ROOT / "weather.db"
        weather_db = candidate if candidate.exists() else None
    build_features(
        archive_dir,
        features_db,
        weather_db,
        args.db_path,
        window_days=args.days,
    )
    return 0
