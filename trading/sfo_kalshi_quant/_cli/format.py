"""Terminal formatting for analysis and paper-trading commands."""

from __future__ import annotations

from ..arbitrage import ArbitrageOpportunity
from ..colors import Color
from ..consensus import MarketConsensus
from ..models import EnsembleSnapshot, IntradaySnapshot
from ..portfolio import PortfolioPlan
from ..tail_basket import TailBasket


def _fmt_opt(value, spec: str) -> str:
    if value is None:
        return "n/a"
    return spec.format(value)


def _format_pnl(value) -> str:
    if value is None:
        return "open"
    return f"${float(value):.2f}"


def _print_consensus_line(
    consensus: MarketConsensus | None,
    forecast_high_f: float,
    color: Color,
) -> None:
    """One-line "what the market forecasts" summary under the model forecast.

    This is the same headline number Kalshi prints on the market ("70.7
    forecast"), rebuilt from the ladder, shown with its spread, modal bin, and
    the signed gap to our model right where the model's own forecast prints.
    """

    if consensus is None or not consensus.available or consensus.implied_high_f is None:
        return
    pieces = [f"{consensus.implied_high_f:.1f}F"]
    if (
        consensus.p10_f is not None
        and consensus.median_f is not None
        and consensus.p90_f is not None
    ):
        pieces.append(
            f"P10/P50/P90={consensus.p10_f:.1f}/{consensus.median_f:.1f}/{consensus.p90_f:.1f}F"
        )
    if consensus.modal_bin_label:
        pieces.append(f"modal={consensus.modal_bin_label} {consensus.modal_probability:.0%}")
    if consensus.implied_stdev_f is not None:
        pieces.append(f"implied_spread={consensus.implied_stdev_f:.1f}F")
    gap = consensus.gap_to_forecast_f(forecast_high_f)
    line = color.cyan("kalshi forecast: " + " ".join(pieces))
    if gap is not None:
        direction = "warmer than" if gap > 0 else "cooler than" if gap < 0 else "level with"
        gap_text = f"model {gap:+.1f}F ({direction} market)"
        # Flag a material disagreement: that is both the edge source and the risk.
        gap_render = color.yellow(gap_text) if abs(gap) >= 2.0 else color.gray(gap_text)
        line = f"{line} {color.gray('|')} {gap_render}"
    print(line)


def _print_analysis(
    event_title,
    forecast,
    decisions,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    paper_stake: float | None = None,
    daily_budget: float | None = None,
    daily_budget_remaining: float | None = None,
    intraday: IntradaySnapshot | None = None,
    ensemble: EnsembleSnapshot | None = None,
    entry_block_reason: str | None = None,
    consensus: MarketConsensus | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    print(
        f"{color.bold('forecast')} {forecast.target_date.isoformat()}: {forecast.predicted_high_f:.2f}F "
        f"source_spread={forecast.source_spread_f:.2f}F method={forecast.method}"
    )
    _print_consensus_line(consensus, forecast.predicted_high_f, color)
    forecast_context = _forecast_context_pieces(forecast)
    if forecast_context:
        print(color.cyan("forecast context: " + "; ".join(forecast_context)))
    intraday_update = forecast.raw.get("intraday_update") if isinstance(forecast.raw, dict) else None
    if intraday is not None and intraday.observed_high_f is not None:
        pieces = [f"observed_high_so_far={intraday.observed_high_f:.1f}F"]
        if intraday.observed_high_source:
            pieces.append(f"source={intraday.observed_high_source}")
        if intraday.is_complete:
            pieces.append("complete_daily_high")
        if intraday.latest_temp_f is not None:
            pieces.append(f"latest_temp={intraday.latest_temp_f:.1f}F")
        if intraday.remaining_forecast_high_f is not None:
            pieces.append(f"remaining_hourly_high={intraday.remaining_forecast_high_f:.1f}F")
        if intraday_update:
            pieces.append(
                f"adjusted_from={float(intraday_update['pre_intraday_predicted_high_f']):.2f}F"
            )
        print(color.cyan("intraday: " + "; ".join(pieces)))
    observed_decision = forecast.raw.get("observed_high_decision") if isinstance(forecast.raw, dict) else None
    if isinstance(observed_decision, dict):
        mode = observed_decision.get("mode")
        reason = observed_decision.get("reason")
        high = observed_decision.get("highF")
        if mode and reason and high is not None:
            print(color.cyan(f"observed lock: {mode} at {float(high):.1f}F ({reason})"))
    if ensemble is not None:
        grid = "-"
        if ensemble.grid_latitude is not None and ensemble.grid_longitude is not None:
            grid = f"{ensemble.grid_latitude:.2f},{ensemble.grid_longitude:.2f}"
        print(
            color.cyan(
                "ensemble: "
                f"station_mean={ensemble.station_mean_high_f:.2f}F "
                f"raw_mean={ensemble.raw_mean_high_f:.2f}F "
                f"station_std={ensemble.station_std_high_f:.2f}F "
                f"members={ensemble.member_count} "
                f"cell={ensemble.cell_selection} "
                f"grid={grid} "
                f"station_shift={ensemble.station_bias_f:+.2f}F"
            )
        )
        if ensemble.warning:
            print(color.yellow(f"ensemble warning: {ensemble.warning}"))
    if paper_stake is not None:
        print(color.yellow(f"paper stake override: ${paper_stake:.2f} per approved trade"))
    if daily_budget is not None:
        remaining = daily_budget if daily_budget_remaining is None else daily_budget_remaining
        print(
            color.yellow(
                f"daily paper budget: ${daily_budget:.2f} total; "
                f"${remaining:.2f} remaining for this target date"
            )
        )
    if entry_block_reason:
        print(color.yellow(entry_block_reason))
    print("")
    if not market_available:
        print(color.gray("side label          resid  ens   intra model  p     p_lcb heat  q     note"))
        print(color.gray("-" * 103))
        for decision in decisions:
            print(
                f"{decision.side:4s} {decision.label[:13]:13s} "
                f"{_color_prob_optional(color, decision.residual_probability)} "
                f"{_color_prob_optional(color, decision.ensemble_probability)} "
                f"{_color_prob_optional(color, decision.intraday_probability)} "
                f"{_color_prob_optional(color, decision.model_probability)} "
                f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
                f"{_color_prob_optional(color, decision.remaining_heat_risk)} "
                f"{decision.trade_quality_score:5.1f} "
                f"{color.yellow('no active Kalshi market')}"
            )
        return

    print(color.gray("side label          bid   ask resid  ens   intra model  mkt    p     p_lcb heat  edge  edge_lcb q     contracts spend    decision"))
    print(color.gray("-" * 158))
    for decision in decisions:
        status = color.green(color.bold("TRADE")) if decision.approved else color.red("NO")
        reason = "" if decision.approved else color.gray("; ".join(decision.reasons[:2]))
        spend = decision.recommended_contracts * decision.cost_per_contract
        print(
            f"{decision.side:4s} {decision.label[:13]:13s} "
            f"{decision.bid:5.2f} {decision.ask:5.2f} "
            f"{_color_prob_optional(color, decision.residual_probability)} "
            f"{_color_prob_optional(color, decision.ensemble_probability)} "
            f"{_color_prob_optional(color, decision.intraday_probability)} "
            f"{_color_prob_optional(color, decision.model_probability)} "
            f"{_color_prob_optional(color, decision.market_probability)} "
            f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
            f"{_color_prob_optional(color, decision.remaining_heat_risk)} "
            f"{_color_edge(color, decision.edge)} {_color_edge(color, decision.edge_lcb)} "
            f"{decision.trade_quality_score:5.1f} "
            f"{decision.recommended_contracts:9.4f} ${spend:7.2f} {status} {reason}"
        )
    if placed_ids:
        print("")
        print(color.green(f"recorded paper orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _print_portfolio_scan(
    event_title,
    forecast,
    plan: PortfolioPlan,
    decisions,
    *,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    intraday: IntradaySnapshot | None = None,
    ensemble: EnsembleSnapshot | None = None,
    entry_block_reason: str | None = None,
    consensus: MarketConsensus | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    print(
        f"{color.bold('portfolio scan')} {forecast.target_date.isoformat()}: "
        f"forecast={forecast.predicted_high_f:.2f}F "
        f"source_spread={forecast.source_spread_f:.2f}F method={forecast.method} "
        f"profile={plan.risk_profile}"
    )
    _print_consensus_line(consensus, forecast.predicted_high_f, color)
    forecast_context = _forecast_context_pieces(forecast)
    if forecast_context:
        print(color.cyan("forecast context: " + "; ".join(forecast_context)))
    if intraday is not None and intraday.observed_high_f is not None:
        pieces = [f"observed_high_so_far={intraday.observed_high_f:.1f}F"]
        if intraday.is_complete:
            pieces.append("complete_daily_high")
        if intraday.latest_temp_f is not None:
            pieces.append(f"latest_temp={intraday.latest_temp_f:.1f}F")
        print(color.cyan("intraday: " + "; ".join(pieces)))
    if ensemble is not None:
        print(
            color.cyan(
                "ensemble: "
                f"station_mean={ensemble.station_mean_high_f:.2f}F "
                f"station_std={ensemble.station_std_high_f:.2f}F "
                f"members={ensemble.member_count}"
            )
        )
        if ensemble.warning:
            print(color.yellow(f"ensemble warning: {ensemble.warning}"))
    if not market_available:
        print(color.yellow("no active Kalshi market; portfolio placement is disabled"))
    if entry_block_reason:
        print(color.yellow(entry_block_reason))

    if entry_block_reason:
        blocked_label = (
            "BLOCKED_BY_PAUSE"
            if "paused" in entry_block_reason.lower()
            else "BLOCKED"
        )
        status = color.yellow(color.bold(blocked_label))
    else:
        status = color.green(color.bold("APPROVED")) if plan.approved else color.red(color.bold("REJECTED"))
    print("")
    print(
        f"portfolio={status} run={plan.run_id} "
        f"spend=${plan.total_spend:.2f} expected=${plan.expected_profit:.2f} "
        f"worst_loss=${plan.worst_case_loss:.2f} "
        f"loss_cap=${plan.limits.max_daily_loss:.2f} "
        f"yes_sleeve=${plan.limits.yes_sleeve:.2f} "
        f"explore_sleeve=${plan.limits.explore_sleeve:.2f}"
    )
    for reason in plan.reasons:
        print(color.yellow(f"allocator: {reason}"))

    print("")
    print(color.gray("sleeve            side label          bid   ask    p   p_lcb  edge edge_lcb q     contracts spend    decision"))
    print(color.gray("-" * 124))
    sleeve_by_key = {
        _portfolio_decision_key(leg.decision): leg.sleeve
        for leg in plan.legs
    }
    for decision in decisions:
        sleeve = sleeve_by_key.get(_portfolio_decision_key(decision), "-")
        status_text = color.green("TRADE") if decision.approved else color.red("NO")
        reason = "" if decision.approved else color.gray("; ".join(decision.reasons[:2]))
        spend = decision.recommended_contracts * decision.cost_per_contract
        print(
            f"{sleeve[:16]:16s} {decision.side:4s} {decision.label[:13]:13s} "
            f"{decision.bid:5.2f} {decision.ask:5.2f} "
            f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
            f"{_color_edge(color, decision.edge)} {_color_edge(color, decision.edge_lcb)} "
            f"{decision.trade_quality_score:5.1f} "
            f"{decision.recommended_contracts:9.4f} ${spend:7.2f} {status_text} {reason}"
        )

    if placed_ids:
        print("")
        print(color.green(f"recorded paper portfolio orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _print_tail_basket(
    event_title,
    forecast,
    basket: TailBasket,
    *,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    intraday: IntradaySnapshot | None = None,
    ensemble: EnsembleSnapshot | None = None,
    entry_block_reason: str | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    print(
        f"{color.bold('tail basket')} {forecast.target_date.isoformat()}: "
        f"forecast={forecast.predicted_high_f:.2f}F "
        f"tail_band={basket.plausible_low_f:.1f}-{basket.plausible_high_f:.1f}F "
        f"source_spread={forecast.source_spread_f:.2f}F method={forecast.method}"
    )
    forecast_context = _forecast_context_pieces(forecast)
    if forecast_context:
        print(color.cyan("forecast context: " + "; ".join(forecast_context)))
    if intraday is not None and intraday.observed_high_f is not None:
        pieces = [f"observed_high_so_far={intraday.observed_high_f:.1f}F"]
        if intraday.is_complete:
            pieces.append("complete_daily_high")
        if intraday.latest_temp_f is not None:
            pieces.append(f"latest_temp={intraday.latest_temp_f:.1f}F")
        print(color.cyan("intraday: " + "; ".join(pieces)))
    if ensemble is not None:
        print(
            color.cyan(
                "ensemble: "
                f"station_mean={ensemble.station_mean_high_f:.2f}F "
                f"station_std={ensemble.station_std_high_f:.2f}F "
                f"members={ensemble.member_count}"
            )
        )
        if ensemble.warning:
            print(color.yellow(f"ensemble warning: {ensemble.warning}"))
    if entry_block_reason:
        print(color.yellow(entry_block_reason))
    if not market_available:
        print(color.yellow("no active Kalshi market; basket is research-only until the event is listed"))

    status = color.green(color.bold("APPROVED")) if basket.approved else color.red(color.bold("REJECTED"))
    print("")
    print(
        f"basket={status} center={basket.center_label or '-'} "
        f"tail_p={basket.tail_yes_probability:.3f} "
        f"spend=${basket.total_spend:.2f} "
        f"edge=${basket.expected_profit:.2f} "
        f"worst_loss=${basket.worst_case_loss:.2f}"
    )
    for reason in basket.reasons:
        print(color.yellow(f"guardrail: {reason}"))

    print("")
    print(color.gray("kind       side label          bid   ask    p   p_lcb  edge edge_lcb contracts spend    decision"))
    print(color.gray("-" * 112))
    for leg in basket.legs:
        decision = leg.decision
        leg_status = color.green("TRADE") if decision.approved and basket.approved else color.red("NO")
        reason = "" if decision.approved else color.gray("; ".join(decision.reasons[:2]))
        print(
            f"{leg.kind:10s} {decision.side:4s} {decision.label[:13]:13s} "
            f"{decision.bid:5.2f} {decision.ask:5.2f} "
            f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
            f"{_color_edge(color, decision.edge)} {_color_edge(color, decision.edge_lcb)} "
            f"{decision.recommended_contracts:9.4f} ${leg.spend:7.2f} {leg_status} {reason}"
        )

    if basket.scenarios:
        print("")
        print(color.gray("settlement scenario       p_yes    basket_pnl"))
        print(color.gray("-" * 48))
        for scenario in basket.scenarios:
            pnl = f"${scenario.pnl:+.2f}"
            pnl = color.green(pnl) if scenario.pnl >= 0 else color.red(pnl)
            p = "-" if scenario.probability is None else f"{scenario.probability:5.3f}"
            print(f"{scenario.label[:22]:22s} {p:>6s} {pnl:>12s}")

    if placed_ids:
        print("")
        print(color.green(f"recorded paper basket orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _print_arbitrage(
    event_title,
    target_date: str,
    opportunities: list[ArbitrageOpportunity],
    *,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    max_spend: float | None,
    min_profit: float,
    entry_block_reason: str | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    spend_text = "profile event cap" if max_spend is None else f"${max_spend:.2f}"
    print(
        f"{color.bold('arbitrage scan')} {target_date}: "
        f"max_spend={spend_text} min_profit=${min_profit:.2f}"
    )
    if not market_available:
        print(color.yellow("no active Kalshi market; arbitrage placement is disabled"))
    if entry_block_reason:
        print(color.yellow(entry_block_reason))

    print("")
    print(color.gray("kind             legs contracts spend    payout   profit   roi     decision"))
    print(color.gray("-" * 88))
    if not opportunities:
        print(color.yellow("no arbitrage portfolios could be evaluated"))
        return

    for opportunity in opportunities:
        status = color.green(color.bold("TRADE")) if opportunity.approved else color.red("NO")
        reason = "" if opportunity.approved else color.gray("; ".join(opportunity.reasons[:2]))
        roi = opportunity.return_on_spend * 100.0
        print(
            f"{opportunity.kind:16s} {len(opportunity.legs):4d} "
            f"{opportunity.contracts:9.4f} ${opportunity.total_spend:7.2f} "
            f"${opportunity.guaranteed_payout:7.2f} ${opportunity.guaranteed_profit:7.2f} "
            f"{roi:6.2f}% {status} {reason}"
        )
        if opportunity.approved:
            for leg in opportunity.legs:
                print(
                    color.gray(
                        f"  {leg.side:3s} {leg.market.yes_sub_title[:18]:18s} "
                        f"ask={leg.price:.2f} fee={leg.fee_per_contract:.4f} "
                        f"cost={leg.cost_per_contract:.4f}"
                    )
                )

    if placed_ids:
        print("")
        print(color.green(f"recorded paper arbitrage orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _color_prob(color: Color, value: float) -> str:
    text = f"{float(value):5.3f}"
    if value >= 0.25:
        return color.green(text)
    if value >= 0.12:
        return color.yellow(text)
    return color.red(text)


def _forecast_context_pieces(forecast) -> list[str]:
    pieces: list[str] = []
    if forecast.lead_hours is not None:
        pieces.append(f"lead={forecast.lead_hours:.1f}h")
    if forecast.fresh_station_count is not None:
        pieces.append(f"fresh_stations={forecast.fresh_station_count}")
    google_api = forecast.raw.get("google_weather_api") if isinstance(forecast.raw, dict) else None
    if isinstance(google_api, dict):
        daily_used = google_api.get("daily_events_used")
        daily_budget = google_api.get("daily_event_budget")
        monthly_used = google_api.get("monthly_events_used")
        monthly_budget = google_api.get("monthly_event_budget")
        if daily_used is not None and daily_budget is not None:
            text = f"google_events={int(daily_used)}/{int(daily_budget)} day"
            if monthly_used is not None and monthly_budget is not None:
                text += f", {int(monthly_used)}/{int(monthly_budget)} month"
            pieces.append(text)
    elif forecast.calls_used_today is not None and forecast.max_calls_per_day is not None:
        pieces.append(f"google_events={forecast.calls_used_today}/{forecast.max_calls_per_day}")
    google_components = forecast.raw.get("google_components") if isinstance(forecast.raw, dict) else None
    if isinstance(google_components, dict):
        hourly = google_components.get("hourly_local_day_high_f")
        daily = google_components.get("daily_endpoint_high_f")
        gap = google_components.get("daily_minus_hourly_gap_f")
        if hourly is not None and daily is not None:
            pieces.append(
                f"google_hourly={float(hourly):.1f}F daily={float(daily):.1f}F gap={float(gap or 0):+.1f}F"
            )
        current = google_components.get("current_conditions")
        if isinstance(current, dict):
            current_temp = current.get("current_temp_f")
            last_24h_max = current.get("last_24h_max_temp_f")
            humidity = current.get("relative_humidity_pct")
            context = []
            if current_temp is not None:
                context.append(f"current={float(current_temp):.1f}F")
            if last_24h_max is not None:
                context.append(f"24h_max={float(last_24h_max):.1f}F")
            if humidity is not None:
                context.append(f"rh={int(humidity)}%")
            if context:
                pieces.append("google_current=" + ",".join(context))
    google_warning = forecast.raw.get("google_warning") if isinstance(forecast.raw, dict) else None
    if google_warning:
        pieces.append(f"google_warning={google_warning}")
    weights = [
        ("G", forecast.google_weight),
        ("NWS", forecast.nws_weight),
        ("OM", forecast.open_meteo_weight),
        ("Hist", forecast.history_weight),
    ]
    if any(value is not None for _, value in weights):
        pieces.append(
            "weights="
            + ",".join(
                f"{label}:{float(value):.2f}"
                for label, value in weights
                if value is not None
            )
        )
    blend_weighting = forecast.raw.get("blend_weighting") if isinstance(forecast.raw, dict) else None
    if isinstance(blend_weighting, dict) and blend_weighting.get("mode"):
        pieces.append(f"weight_mode={blend_weighting['mode']}")
    return pieces


def _color_prob_optional(color: Color, value: float | None) -> str:
    if value is None:
        return color.gray("  n/a")
    return _color_prob(color, value)


def _color_edge(color: Color, value: float) -> str:
    text = f"{float(value):7.3f}"
    if value > 0:
        return color.green(text)
    if value > -0.02:
        return color.yellow(text)
    return color.red(text)


def _color_status(color: Color, status: str) -> str:
    if status in {"PAPER_FILLED", "PAPER_SETTLED"}:
        return color.green(status)
    if status == "PAPER_LIMIT_RESTING":
        return color.yellow(status)
    if status == "PAPER_CLOSED":
        return color.cyan(status)
    if status == "REJECTED":
        return color.red(status)
    return color.yellow(status)

