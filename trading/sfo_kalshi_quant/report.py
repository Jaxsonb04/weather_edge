from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError

from .backtest import DEFAULT_CALIBRATION_CACHE_DIR, run_walk_forward_calibration_backtest
from .config import SERIES_TICKER, StrategyConfig, intraday_timezone_for_city
from .consensus import MarketConsensus, build_market_consensus
from .ensemble import OpenMeteoEnsembleError, SfoEnsembleClient
from .forecast import (
    ForecastDataError,
    SfoForecasterAdapter,
    has_forecaster_observed_high_adjustment,
    parse_target_date,
)
from .kalshi import KalshiPublicClient, load_event_snapshots
from .models import (
    EnsembleSnapshot,
    EventSnapshot,
    ForecastSnapshot,
    IntradaySnapshot,
    TradeDecision,
    format_event_date_token,
)
from .probability import ResidualCalibrator
from .risk import TradeEvaluator
from .settlement_day import settlement_today
from .standard_bins import standard_sfo_bins


def build_daily_report(
    *,
    forecaster_root: Path,
    targets: list[date],
    config: StrategyConfig,
    side: str,
    offline_events: Path | None = None,
    observed_high: float | None = None,
    no_ensemble: bool = False,
    ensemble_timeout: float = 12.0,
    allow_live_market: bool = True,
    calibration_min_train: int = 180,
    calibration_source: str = "auto",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a public, paper-only daily report without recording local state."""

    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    generated_at = generated_at.astimezone(timezone.utc)
    adapter = SfoForecasterAdapter(forecaster_root)
    outcomes = adapter.load_calibration_outcomes(calibration_source)
    calibrator = ResidualCalibrator(outcomes, config)
    calibration = calibration_diagnostics(outcomes, config=config, min_train=calibration_min_train)
    target_reports = [
        build_target_report(
            target=target,
            adapter=adapter,
            calibrator=calibrator,
            config=config,
            side=side,
            offline_events=offline_events,
            observed_high=observed_high,
            no_ensemble=no_ensemble,
            ensemble_timeout=ensemble_timeout,
            allow_live_market=allow_live_market,
        )
        for target in targets
    ]
    settlement_day = settlement_today(generated_at)
    for report in target_reports:
        report["target_status"] = _target_status(
            date.fromisoformat(report["target_date"]),
            settlement_day,
        )
    best = _best_signal(target_reports)
    market_data_at = _latest_timestamp(
        report.get("market_data_at") for report in target_reports
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_data_at": market_data_at,
        "mode": "paper_research_only",
        "live_orders_enabled": False,
        "summary": {
            "best_signal": best,
            "target_count": len(target_reports),
            "approved_signal_count": sum(
                1
                for report in target_reports
                for decision in report["decisions"]
                if decision["approved"]
            ),
        },
        "calibration": calibration,
        "targets": target_reports,
        "disclaimer": "Paper-trading research only. This report does not place live orders.",
    }


def build_target_report(
    *,
    target: date,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    side: str,
    offline_events: Path | None,
    observed_high: float | None,
    no_ensemble: bool,
    ensemble_timeout: float,
    allow_live_market: bool,
) -> dict[str, Any]:
    forecast = adapter.latest_blend(target)
    _enforce_live_forecast_freshness(forecast, config)
    intraday = _intraday_for_report(target, adapter, observed_high)
    observed_high_f = intraday.observed_high_f if intraday else None
    if intraday is not None and not has_forecaster_observed_high_adjustment(forecast):
        forecast = adapter.apply_intraday_update(forecast, intraday)

    ensemble, ensemble_warning = _ensemble_for_report(
        target,
        forecast.predicted_high_f,
        no_ensemble=no_ensemble,
        timeout=ensemble_timeout,
    )
    event, market_warning = _event_for_report(
        target,
        offline_events=offline_events,
        allow_live_market=allow_live_market,
    )
    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
        market_available = True
    else:
        markets = standard_sfo_bins(f"{SERIES_TICKER}-{format_event_date_token(target)}-PAPER")
        event_title = "No live prediction market event found; probability-only fallback ladder"
        market_available = False

    # lead_days=None: read the live EMOS across leads (next-day=1, 2-day-out=2);
    # a fixed lead 1 would drop the 2-day-out market from the published signal.
    emos_lookup = (
        adapter.load_emos_mu_sigma(lead_days=None) if config.emos_distribution_enabled else {}
    )
    probabilities = calibrator.bucket_probabilities(
        markets,
        forecast.predicted_high_f,
        source_spread_f=forecast.source_spread_f,
        observed_high_f=observed_high_f,
        ensemble=ensemble,
        intraday=intraday,
        emos_mu_sigma=emos_lookup.get(target),
        standard_timezone=intraday_timezone_for_city(adapter.city),
    )
    decisions = TradeEvaluator(config).rank(
        markets,
        probabilities,
        bankroll=config.paper_bankroll,
        sides=_analysis_sides(side),
        source_spread_f=forecast.source_spread_f,
    )
    # The market's own forecast, distilled from the live ladder. Only meaningful
    # when a real Kalshi book is present (the fallback paper ladder has no
    # prices), so it is surfaced as unavailable on the probability-only path.
    # NOTE: this lighter public report surfaces the consensus for DISPLAY but,
    # like comfort-edge, does not thread it (or forecast_high_f) into rank()'s
    # sizing -- the authoritative consensus-guard sizing is applied in the live
    # scan path (cli._analyze_one_target) and recorded to the paper DB that the
    # Strategy Lab reads, so the dashboard's decisions reflect the guard.
    consensus = build_market_consensus(markets) if market_available else None
    warnings = [warning for warning in (ensemble_warning, market_warning) if warning]
    return {
        "target_date": target.isoformat(),
        "event_title": event_title,
        "market_available": market_available,
        "market_data_at": _market_data_at(event),
        "forecast": forecast_to_dict(forecast),
        "intraday": intraday_to_dict(intraday),
        "ensemble": ensemble_to_dict(ensemble),
        "market_consensus": consensus_to_dict(
            consensus, forecast.predicted_high_f, probabilities
        ),
        "warnings": warnings,
        "best_decision": decision_to_dict(decisions[0]) if decisions else None,
        "decisions": [decision_to_dict(decision) for decision in decisions],
    }


def calibration_diagnostics(
    outcomes,
    *,
    config: StrategyConfig,
    min_train: int = 180,
    cache_dir: Path | None = DEFAULT_CALIBRATION_CACHE_DIR,
) -> dict[str, Any]:
    try:
        result = run_walk_forward_calibration_backtest(
            outcomes, config=config, min_train=min_train, cache_dir=cache_dir
        )
    except ValueError as exc:
        # Below min_train the walk-forward backtest raises. Emit an empty but
        # well-formed calibration block so the whole daily-report artifact still
        # renders instead of aborting on a thin clean-blend history.
        return {
            "source": outcomes[0].model_name if outcomes else "unknown",
            "n": 0,
            "available": False,
            "reason": "insufficient_history",
            "detail": str(exc),
            "min_train": min_train,
            "outcomes": len(outcomes),
            "brier_score": None,
            "ranked_probability_score": None,
            "ranked_probability_skill": None,
            "log_loss": None,
            "top_bin_accuracy": None,
            "avg_winning_probability": None,
            "avg_entropy": None,
            "buckets": [],
            "cohorts": [],
            "warnings": [],
            "cache_hit": False,
        }
    buckets = [
        {
            "range": f"{bucket.lower:.1f}-{bucket.upper:.1f}",
            "lower": bucket.lower,
            "upper": bucket.upper,
            "count": bucket.count,
            "avg_probability": round(bucket.avg_probability, 4),
            "observed_frequency": round(bucket.observed_frequency, 4),
            "calibration_gap": round(bucket.observed_frequency - bucket.avg_probability, 4),
            "brier_score": round(bucket.brier_score, 4),
        }
        for bucket in result.calibration_buckets
    ]
    cohorts = [
        {
            "name": cohort.name,
            "count": cohort.count,
            "brier_score": round(cohort.brier_score, 4),
            "ranked_probability_score": round(cohort.ranked_probability_score, 4),
            "climatology_ranked_probability_score": round(
                cohort.climatology_ranked_probability_score,
                4,
            ),
            "ranked_probability_skill": round(cohort.ranked_probability_skill, 4),
            "log_loss": round(cohort.log_loss, 4),
            "top_bin_accuracy": round(cohort.top_bin_accuracy, 4),
            "avg_winning_probability": round(cohort.avg_winning_probability, 4),
        }
        for cohort in result.cohorts
    ]
    warnings = []
    for bucket in buckets:
        if bucket["count"] < 15:
            continue
        if 0.5 <= bucket["lower"] < 0.7 and bucket["calibration_gap"] <= -0.10:
            warnings.append(
                "Mid-probability buckets are overconfident: "
                f"{bucket['range']} averages {bucket['avg_probability']:.3f} "
                f"but resolves {bucket['observed_frequency']:.3f}."
            )
    return {
        "source": outcomes[0].model_name if outcomes else "unknown",
        "n": result.n,
        "brier_score": round(result.brier_score, 4),
        "climatology_brier_score": round(result.climatology_brier_score, 4),
        "brier_skill": round(result.brier_skill, 4),
        "ranked_probability_score": round(result.ranked_probability_score, 4),
        "climatology_ranked_probability_score": round(
            result.climatology_ranked_probability_score,
            4,
        ),
        "ranked_probability_skill": round(result.ranked_probability_skill, 4),
        "log_loss": round(result.log_loss, 4),
        "top_bin_accuracy": round(result.top_bin_accuracy, 4),
        "avg_winning_probability": round(result.avg_winning_probability, 4),
        "avg_entropy": round(result.avg_entropy, 4),
        "buckets": buckets,
        "cohorts": cohorts,
        "warnings": warnings,
        "cache_hit": result.cache_hit,
    }


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a concurrent reader/publisher never
    observes a half-written artifact (forecaster-refresh and strategy-lab-refresh
    build into the same shared dir; os.replace is atomic on one filesystem)."""

    import os

    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def forecast_to_dict(forecast: ForecastSnapshot) -> dict[str, Any]:
    return {
        "target_date": forecast.target_date.isoformat(),
        "predicted_high_f": round(forecast.predicted_high_f, 2),
        "source_spread_f": round(forecast.source_spread_f, 2),
        "fetched_at": forecast.fetched_at,
        "lead_hours": forecast.lead_hours,
        "method": forecast.method,
        "source_count": forecast.source_count,
        "fresh_station_count": forecast.fresh_station_count,
        "calls_used_today": forecast.calls_used_today,
        "max_calls_per_day": forecast.max_calls_per_day,
        "sources": {
            "google_high_f": forecast.google_high_f,
            "nws_high_f": forecast.nws_high_f,
            "open_meteo_high_f": forecast.open_meteo_high_f,
            "history_high_f": forecast.history_high_f,
        },
    }


def intraday_to_dict(intraday: IntradaySnapshot | None) -> dict[str, Any] | None:
    if intraday is None:
        return None
    return {
        "target_date": intraday.target_date.isoformat(),
        "observed_high_f": intraday.observed_high_f,
        "latest_temp_f": intraday.latest_temp_f,
        "latest_observed_at": intraday.latest_observed_at,
        "remaining_forecast_high_f": intraday.remaining_forecast_high_f,
        "forecast_fetched_at": intraday.forecast_fetched_at,
        "observation_count": intraday.observation_count,
        "observed_high_source": intraday.observed_high_source,
        "is_complete": intraday.is_complete,
    }


def ensemble_to_dict(ensemble: EnsembleSnapshot | None) -> dict[str, Any] | None:
    if ensemble is None:
        return None
    return {
        "source": ensemble.source,
        "member_count": ensemble.member_count,
        "station_mean_high_f": round(ensemble.station_mean_high_f, 2),
        "station_std_high_f": round(ensemble.station_std_high_f, 2),
        "station_bias_f": round(ensemble.station_bias_f, 2),
        "cell_selection": ensemble.cell_selection,
        "warning": ensemble.warning,
    }


def consensus_to_dict(
    consensus: MarketConsensus | None,
    forecast_high_f: float,
    probabilities: dict[str, Any],
) -> dict[str, Any]:
    """Serialize the market-implied consensus forecast for the public report.

    Includes a per-bin distribution that pairs the de-vigged market probability
    with our model probability so the dashboard can overlay "what the market
    thinks" against "what the model thinks" directly.
    """

    if consensus is None or not consensus.available or consensus.implied_high_f is None:
        return {"available": False}

    distribution = []
    for bucket in consensus.bins:
        probability = probabilities.get(bucket.ticker)
        model_p = getattr(probability, "model_probability", None)
        distribution.append(
            {
                "ticker": bucket.ticker,
                "label": bucket.label,
                "center_f": round(bucket.center_f, 1) if math.isfinite(bucket.center_f) else None,
                "implied_probability": round(bucket.implied_probability, 4),
                "model_probability": round(model_p, 4) if model_p is not None else None,
            }
        )

    gap = consensus.gap_to_forecast_f(forecast_high_f)
    return {
        "available": True,
        "implied_high_f": round(consensus.implied_high_f, 2),
        "model_high_f": round(forecast_high_f, 2),
        "model_minus_market_f": round(gap, 2) if gap is not None else None,
        "modal_bin_ticker": consensus.modal_bin_ticker,
        "modal_bin_label": consensus.modal_bin_label,
        "modal_probability": round(consensus.modal_probability, 4),
        "implied_stdev_f": _round_optional(consensus.implied_stdev_f),
        "p10_f": _round_optional(consensus.p10_f),
        "p25_f": _round_optional(consensus.p25_f),
        "median_f": _round_optional(consensus.median_f),
        "p75_f": _round_optional(consensus.p75_f),
        "p90_f": _round_optional(consensus.p90_f),
        "overround": round(consensus.overround, 4),
        "liquid_bin_count": consensus.liquid_bin_count,
        "distribution": distribution,
    }


def decision_to_dict(decision: TradeDecision) -> dict[str, Any]:
    spend = decision.recommended_contracts * decision.cost_per_contract
    return {
        "ticker": decision.ticker,
        "label": decision.label,
        "side": decision.side,
        "action": decision.action,
        "approved": decision.approved,
        "decision": "TRADE" if decision.approved else "NO_TRADE",
        "probability": round(decision.probability, 4),
        "probability_lcb": round(decision.probability_lcb, 4),
        "model_probability": _round_optional(decision.model_probability),
        "market_probability": _round_optional(decision.market_probability),
        "residual_probability": _round_optional(decision.residual_probability),
        "ensemble_probability": _round_optional(decision.ensemble_probability),
        "intraday_probability": _round_optional(decision.intraday_probability),
        "remaining_heat_risk": _round_optional(decision.remaining_heat_risk),
        "bid": round(decision.bid, 4),
        "ask": round(decision.ask, 4),
        "spread": round(decision.spread, 4),
        "edge": round(decision.edge, 4),
        "edge_lcb": round(decision.edge_lcb, 4),
        "trade_quality_score": round(decision.trade_quality_score, 1),
        "recommended_contracts": round(decision.recommended_contracts, 4),
        "recommended_spend": round(spend, 2),
        "expected_profit": round(decision.expected_profit, 4),
        "reasons": list(decision.reasons),
    }


def _event_for_report(
    target: date,
    *,
    offline_events: Path | None,
    allow_live_market: bool,
) -> tuple[EventSnapshot | None, str | None]:
    if offline_events:
        events = load_event_snapshots(offline_events, target)
        return (events[0] if events else None), None
    if not allow_live_market:
        return (
            None,
            "Live prediction market lookup disabled; using probability-only fallback ladder.",
        )
    try:
        return KalshiPublicClient().find_event_by_date(target, series_ticker=SERIES_TICKER), None
    except URLError as exc:
        return None, f"Live prediction market lookup failed: {exc}"


def _target_status(target: date, today: date) -> str:
    if target < today:
        return "past"
    if target == today:
        return "settlement_day"
    return "upcoming"


def _parse_source_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        return _parse_source_timestamp(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_timestamp(values) -> str | None:
    parsed = [
        timestamp
        for value in values
        if (timestamp := _parse_source_timestamp(value))
    ]
    if not parsed:
        return None
    return max(parsed).isoformat(timespec="seconds")


def _market_data_at(event: EventSnapshot | None) -> str | None:
    """Return the newest source timestamp without inventing one when absent."""

    if event is None:
        return None
    values: list[object] = []
    for payload in [event.raw, *(market.raw for market in event.markets)]:
        for key in ("updated_time", "last_updated_ts", "fetched_at"):
            values.append(payload.get(key))
    return _latest_timestamp(values)


def _ensemble_for_report(
    target: date,
    station_center_high_f: float,
    *,
    no_ensemble: bool,
    timeout: float,
) -> tuple[EnsembleSnapshot | None, str | None]:
    if no_ensemble:
        return None, "Open-Meteo ensemble disabled for this report."
    try:
        return SfoEnsembleClient(timeout=timeout).station_aligned_snapshot(target, station_center_high_f), None
    except (OpenMeteoEnsembleError, OSError, TimeoutError, URLError) as exc:
        return None, f"Station-aligned ensemble lookup failed: {exc}"


def _intraday_for_report(
    target: date,
    adapter: SfoForecasterAdapter,
    observed_high: float | None,
) -> IntradaySnapshot | None:
    if target != parse_target_date("today"):
        return None
    intraday = adapter.intraday_snapshot(target)
    if observed_high is None:
        return intraday
    if intraday is None:
        return IntradaySnapshot(
            target_date=target,
            observed_high_f=observed_high,
            latest_temp_f=None,
            latest_observed_at=None,
            remaining_forecast_high_f=None,
            forecast_fetched_at=None,
            observation_count=0,
        )
    if is_dataclass(intraday):
        values = asdict(intraday)
        values["observed_high_f"] = observed_high
        return IntradaySnapshot(**values)
    return intraday


def _analysis_sides(side_arg: str) -> tuple[str, ...]:
    if side_arg == "both":
        return ("YES", "NO")
    return (side_arg.upper(),)


def _best_signal(target_reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    decisions = [
        {**decision, "target_date": report["target_date"], "market_available": report["market_available"]}
        for report in target_reports
        for decision in report["decisions"]
    ]
    if not decisions:
        return None
    decisions.sort(
        key=lambda row: (
            row["market_available"],
            row["approved"],
            row["trade_quality_score"],
            row["edge_lcb"],
            row["edge"],
        ),
        reverse=True,
    )
    return decisions[0]


def _round_optional(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _enforce_live_forecast_freshness(forecast: ForecastSnapshot, config: StrategyConfig) -> None:
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
