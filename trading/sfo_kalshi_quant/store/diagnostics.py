from __future__ import annotations

import json
import sqlite3

from .._util import (
    _drop_none,
    _json_list,
    _json_object,
    _json_safe_value,
    _optional_float,
    _round_number,
    _row_value as _shared_row_value,
)
from ..config import StrategyConfig
from ..consensus import MarketConsensus
from ..models import EventSnapshot, ForecastSnapshot, IntradaySnapshot, TradeDecision


def _row_value(row: object, key: str, default=None):
    return _shared_row_value(row, key, default, default_on_none=True)


def _row_side(row: sqlite3.Row) -> str:
    try:
        side = row["side"]
    except (IndexError, KeyError):
        side = None
    if side:
        normalized = str(side).upper()
        if normalized in {"YES", "NO"}:
            return normalized
    try:
        action = str(row["action"]).upper()
    except (IndexError, KeyError):
        return "YES"
    return "NO" if "NO" in action else "YES"


def _forecast_observed_high_mode(forecast: ForecastSnapshot | None) -> str | None:
    if forecast is None or not isinstance(forecast.raw, dict):
        return None
    decision = forecast.raw.get("observed_high_decision")
    if not isinstance(decision, dict):
        return None
    mode = decision.get("mode")
    return str(mode).lower() if mode else None


def _market_close_time(raw: dict | None) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("close_time") or raw.get("expected_expiration_time") or raw.get("expiration_time")
    return str(value) if value else None


def _decision_diagnostics_payload(
    target_date: str,
    decision: TradeDecision,
    *,
    created_at: str,
    forecast: ForecastSnapshot | None,
    intraday: IntradaySnapshot | None,
    event: EventSnapshot | None,
    market,
    market_consensus: MarketConsensus | None,
    prediction_features: dict[str, object],
    risk_profile: str | None,
    bankroll: float | None,
    strategy_config: StrategyConfig | None,
    forecast_snapshot_id: int | None,
    market_snapshot_id: int | None,
) -> dict[str, object]:
    return _drop_none(
        {
            "schema_version": 1,
            "kind": "trade_decision",
            "created_at": created_at,
            "target_date": target_date,
            "risk_profile": risk_profile,
            "bankroll": _round_number(bankroll),
            "context_refs": {
                "forecast_snapshot_id": forecast_snapshot_id,
                "market_snapshot_id": market_snapshot_id,
            },
            "signal": _decision_signal_payload(decision),
            "forecast": _forecast_diagnostics_payload(forecast),
            "intraday": _intraday_diagnostics_payload(intraday),
            "market": _market_diagnostics_payload(market, event),
            "market_consensus": _market_consensus_diagnostics_payload(market_consensus),
            "prediction_features": dict(prediction_features or {}),
            "strategy_config": _strategy_config_snapshot(strategy_config),
        }
    )


def _order_entry_diagnostics_payload(
    target_date: str,
    decision: TradeDecision,
    *,
    created_at: str,
    kind: str,
    risk_profile: str | None,
    status: str,
    entry_mode: str,
    group_id: str | None,
    strategy_config: StrategyConfig | None,
    sample_probability: float | None,
    sampled: bool | None,
    entry_decision: sqlite3.Row | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": kind,
        "created_at": created_at,
        "target_date": target_date,
        "risk_profile": risk_profile,
        "status": status,
        "entry_mode": entry_mode,
        "group_id": group_id,
        "signal": _decision_signal_payload(decision),
        "strategy_config": _strategy_config_snapshot(strategy_config),
        "entry_decision": _entry_decision_ref_payload(entry_decision),
    }
    if sample_probability is not None or sampled is not None:
        payload["sampling"] = {
            "sample_probability": _round_number(sample_probability),
            "sampled": sampled,
        }
    return _drop_none(payload)


def _monitor_diagnostics_payload(
    order: sqlite3.Row,
    *,
    created_at: str,
    side: str,
    action: str,
    reason: str | None,
    market_status: str | None,
    live_bid: float | None,
    exit_fee_per_contract: float | None,
    net_exit_per_contract: float | None,
    unrealized_pnl: float | None,
    unrealized_roi: float | None,
) -> dict[str, object]:
    return _drop_none(
        {
            "schema_version": 1,
            "kind": "paper_monitor_snapshot",
            "created_at": created_at,
            "order_id": _row_value(order, "id"),
            "target_date": _row_value(order, "target_date"),
            "market_ticker": _row_value(order, "market_ticker"),
            "risk_profile": _row_value(order, "risk_profile"),
            "side": side.upper(),
            "action": action,
            "reason": reason,
            "market_status": market_status,
            "mark": {
                "live_bid": _round_number(live_bid),
                "exit_fee_per_contract": _round_number(exit_fee_per_contract),
                "net_exit_per_contract": _round_number(net_exit_per_contract),
                "unrealized_pnl": _round_number(unrealized_pnl),
                "unrealized_roi": _round_number(unrealized_roi),
            },
            "entry": _order_entry_snapshot(order),
            "entry_diagnostics": _json_object(_row_value(order, "diagnostics_json")),
        }
    )


def _outcome_diagnostics_payload(
    row: sqlite3.Row,
    *,
    event: str,
    resolved_at: str,
    settlement_high_f: float | None,
    resolved_yes: bool | None,
    position_won: bool | None,
    realized_pnl: float,
    exit_price: float | None = None,
    exit_fee_per_contract: float | None = None,
) -> dict[str, object]:
    entry_diagnostics = _json_object(_row_value(row, "diagnostics_json"))
    entry_decision = entry_diagnostics.get("entry_decision") if isinstance(entry_diagnostics, dict) else None
    source_diagnostics = (
        entry_decision.get("diagnostics")
        if isinstance(entry_decision, dict) and isinstance(entry_decision.get("diagnostics"), dict)
        else entry_diagnostics
    )
    prediction_features = (
        source_diagnostics.get("prediction_features")
        if isinstance(source_diagnostics, dict) and isinstance(source_diagnostics.get("prediction_features"), dict)
        else {}
    )
    predicted_high = _optional_float(prediction_features.get("predicted_high_f"))
    forecast_error = (
        settlement_high_f - predicted_high
        if settlement_high_f is not None and predicted_high is not None
        else None
    )
    side = _row_side(row)
    return _drop_none(
        {
            "schema_version": 1,
            "kind": "paper_order_outcome",
            "entry": {
                "order_id": _row_value(row, "id"),
                "decision_snapshot_id": _row_value(row, "entry_decision_snapshot_id"),
                "created_at": _row_value(row, "created_at"),
                "target_date": _row_value(row, "target_date"),
                "market_ticker": _row_value(row, "market_ticker"),
                "label": _row_value(row, "label"),
                "side": side,
                "risk_profile": _row_value(row, "risk_profile"),
                "entry_price": _round_number(_row_value(row, "entry_price")),
                "cost_per_contract": _round_number(_row_value(row, "cost_per_contract")),
                "contracts": _round_number(_row_value(row, "contracts")),
                "probability": _round_number(_row_value(row, "probability")),
                "probability_lcb": _round_number(_row_value(row, "probability_lcb")),
                "edge": _round_number(_row_value(row, "edge")),
                "edge_lcb": _round_number(_row_value(row, "edge_lcb")),
                "trade_quality_score": _round_number(_row_value(row, "trade_quality_score")),
                "reasons": _json_list(_row_value(row, "reasons_json")),
                "diagnostics": source_diagnostics,
            },
            "outcome": {
                "event": event,
                "resolved_at": resolved_at,
                "settlement_high_f": _round_number(settlement_high_f),
                "resolved_yes": resolved_yes,
                "position_won": position_won,
                "realized_pnl": _round_number(realized_pnl),
                "pnl_per_contract": _round_number(
                    realized_pnl / float(_row_value(row, "contracts", 0.0) or 1.0)
                ),
                "exit_price": _round_number(exit_price),
                "exit_fee_per_contract": _round_number(exit_fee_per_contract),
                "forecast_error_f": _round_number(forecast_error),
                "win_loss_reason": _win_loss_reason(
                    event,
                    side=side,
                    resolved_yes=resolved_yes,
                    position_won=position_won,
                    realized_pnl=realized_pnl,
                ),
            },
        }
    )


def _decision_signal_payload(decision: TradeDecision) -> dict[str, object]:
    return _drop_none(
        {
            "ticker": decision.ticker,
            "label": decision.label,
            "action": decision.action,
            "side": decision.side,
            "approved": bool(decision.approved),
            "signal_approved": (
                bool(decision.signal_approved)
                if decision.signal_approved is not None
                else bool(decision.approved)
            ),
            "entry_block_reason": decision.entry_block_reason,
            "probability": _round_number(decision.probability),
            "probability_lcb": _round_number(decision.probability_lcb),
            "model_probability": _round_number(decision.model_probability),
            "market_probability": _round_number(decision.market_probability),
            "residual_probability": _round_number(decision.residual_probability),
            "ensemble_probability": _round_number(decision.ensemble_probability),
            "intraday_probability": _round_number(decision.intraday_probability),
            "remaining_heat_risk": _round_number(decision.remaining_heat_risk),
            "yes_bid": _round_number(decision.yes_bid),
            "yes_ask": _round_number(decision.yes_ask),
            "entry_bid": _round_number(decision.bid),
            "entry_ask": _round_number(decision.ask),
            "entry_bid_size": _round_number(decision.bid_size),
            "entry_ask_size": _round_number(decision.ask_size),
            "spread": _round_number(decision.spread),
            "fee_per_contract": _round_number(decision.fee_per_contract),
            "cost_per_contract": _round_number(decision.cost_per_contract),
            "edge": _round_number(decision.edge),
            "edge_lcb": _round_number(decision.edge_lcb),
            "kelly_fraction": _round_number(decision.kelly_fraction),
            "recommended_contracts": _round_number(decision.recommended_contracts),
            "recommended_spend": _round_number(
                decision.recommended_contracts * decision.cost_per_contract
            ),
            "expected_profit": _round_number(decision.expected_profit),
            "trade_quality_score": _round_number(decision.trade_quality_score),
            "binding_constraint": decision.binding_constraint,
            "strike_type": decision.strike_type,
            "floor_strike": _round_number(decision.floor_strike),
            "cap_strike": _round_number(decision.cap_strike),
            "limit_price": _round_number(decision.limit_price),
            "limit_fee_per_contract": _round_number(decision.limit_fee_per_contract),
            "limit_cost_per_contract": _round_number(decision.limit_cost_per_contract),
            "limit_edge": _round_number(decision.limit_edge),
            "limit_edge_lcb": _round_number(decision.limit_edge_lcb),
            "reasons": list(decision.reasons),
        }
    )


def _forecast_diagnostics_payload(forecast: ForecastSnapshot | None) -> dict[str, object] | None:
    if forecast is None:
        return None
    return _drop_none(
        {
            "target_date": forecast.target_date.isoformat(),
            "predicted_high_f": _round_number(forecast.predicted_high_f),
            "fetched_at": forecast.fetched_at,
            "lead_hours": _round_number(forecast.lead_hours),
            "method": forecast.method,
            "source_spread_f": _round_number(forecast.source_spread_f),
            "source_count": forecast.source_count,
            "sources": {
                "google_high_f": _round_number(forecast.google_high_f),
                "nws_high_f": _round_number(forecast.nws_high_f),
                "open_meteo_high_f": _round_number(forecast.open_meteo_high_f),
                "history_high_f": _round_number(forecast.history_high_f),
            },
            "weights": {
                "google_weight": _round_number(forecast.google_weight),
                "nws_weight": _round_number(forecast.nws_weight),
                "open_meteo_weight": _round_number(forecast.open_meteo_weight),
                "history_weight": _round_number(forecast.history_weight),
            },
            "station_adjustment_f": _round_number(forecast.station_adjustment_f),
            "fresh_station_count": forecast.fresh_station_count,
            "max_calls_per_day": forecast.max_calls_per_day,
            "calls_used_today": forecast.calls_used_today,
            "raw_feature_keys": sorted(forecast.raw.keys()) if isinstance(forecast.raw, dict) else None,
        }
    )


def _intraday_diagnostics_payload(intraday: IntradaySnapshot | None) -> dict[str, object] | None:
    if intraday is None:
        return None
    return _drop_none(
        {
            "target_date": intraday.target_date.isoformat(),
            "observed_high_f": _round_number(intraday.observed_high_f),
            "latest_temp_f": _round_number(intraday.latest_temp_f),
            "latest_observed_at": intraday.latest_observed_at,
            "remaining_forecast_high_f": _round_number(intraday.remaining_forecast_high_f),
            "forecast_fetched_at": intraday.forecast_fetched_at,
            "observation_count": intraday.observation_count,
            "observed_high_source": intraday.observed_high_source,
            "is_complete": intraday.is_complete,
        }
    )


def _market_diagnostics_payload(market, event: EventSnapshot | None) -> dict[str, object] | None:
    if market is None:
        return _drop_none(
            {
                "event_ticker": event.event_ticker if event is not None else None,
                "event_title": event.title if event is not None else None,
                "target_date": event.target_date.isoformat() if event is not None and event.target_date else None,
            }
        )
    return _drop_none(
        {
            "event_ticker": market.event_ticker,
            "event_title": event.title if event is not None else None,
            "ticker": market.ticker,
            "title": market.title,
            "label": market.yes_sub_title,
            "status": market.status,
            "result": market.result,
            "close_time": _market_close_time(market.raw),
            "strike_type": market.strike_type,
            "floor_strike": _round_number(market.floor_strike),
            "cap_strike": _round_number(market.cap_strike),
            "yes_bid": _round_number(market.yes_bid),
            "yes_ask": _round_number(market.yes_ask),
            "no_bid": _round_number(market.no_bid),
            "no_ask": _round_number(market.no_ask),
            "yes_bid_size": _round_number(market.yes_bid_size),
            "yes_ask_size": _round_number(market.yes_ask_size),
            "no_bid_size": _round_number(market.no_bid_size),
            "no_ask_size": _round_number(market.no_ask_size),
            "spread": _round_number(market.spread),
            "no_spread": _round_number(market.no_spread),
            "expiration_value": _round_number(market.expiration_value),
        }
    )


def _market_diagnostics_from_snapshot_json(
    raw_json: object,
    market_ticker: str,
) -> dict[str, object] | None:
    raw = _json_object(raw_json)
    if not raw:
        return None
    try:
        event = EventSnapshot.from_kalshi(raw)
    except (TypeError, ValueError, KeyError):
        return None
    market = next((item for item in event.markets if item.ticker == market_ticker), None)
    return _market_diagnostics_payload(market, event)


def _market_consensus_diagnostics_payload(
    market_consensus: MarketConsensus | None,
) -> dict[str, object] | None:
    if market_consensus is None:
        return None
    return _drop_none(
        {
            "available": market_consensus.available,
            "implied_high_f": _round_number(market_consensus.implied_high_f),
            "modal_bin_ticker": market_consensus.modal_bin_ticker,
            "modal_bin_label": market_consensus.modal_bin_label,
            "modal_probability": _round_number(market_consensus.modal_probability),
            "implied_stdev_f": _round_number(market_consensus.implied_stdev_f),
            "p10_f": _round_number(market_consensus.p10_f),
            "p25_f": _round_number(market_consensus.p25_f),
            "median_f": _round_number(market_consensus.median_f),
            "p75_f": _round_number(market_consensus.p75_f),
            "p90_f": _round_number(market_consensus.p90_f),
            "overround": _round_number(market_consensus.overround),
            "liquid_bin_count": market_consensus.liquid_bin_count,
        }
    )


def _strategy_config_snapshot(config: StrategyConfig | None) -> dict[str, object] | None:
    if config is None:
        return None
    return {
        key: _json_safe_value(value)
        for key, value in sorted(config.__dict__.items())
    }


def _latest_entry_decision_snapshot(
    conn: sqlite3.Connection,
    target_date: str,
    decision: TradeDecision,
    *,
    risk_profile: str | None,
) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    filters = [
        "d.target_date = ?",
        "d.market_ticker = ?",
        "UPPER(COALESCE(d.side, 'YES')) = ?",
    ]
    params: list[object] = [target_date, decision.ticker, decision.side.upper()]
    if risk_profile is not None:
        filters.append("COALESCE(d.risk_profile, 'live') = ?")
        params.append(risk_profile)
    return conn.execute(
        f"""
        SELECT d.*,
               c.id AS scan_context_joined_id,
               c.schema_version AS scan_context_schema_version,
               c.created_at AS scan_context_created_at,
               c.target_date AS scan_context_target_date,
               c.risk_profile AS scan_context_risk_profile,
               c.bankroll AS scan_context_bankroll,
               c.forecast_snapshot_id AS scan_context_forecast_snapshot_id,
               c.market_snapshot_id AS scan_context_market_snapshot_id,
               c.forecast_json AS scan_context_forecast_json,
               c.intraday_json AS scan_context_intraday_json,
               c.market_json AS scan_context_market_json,
               c.market_consensus_json AS scan_context_market_consensus_json,
               c.prediction_features_json AS scan_context_prediction_features_json,
               c.strategy_config_json AS scan_context_strategy_config_json,
               ms.raw_json AS scan_context_market_snapshot_json
        FROM decision_snapshots d
        LEFT JOIN scan_context_snapshots c ON c.id = d.scan_context_id
        LEFT JOIN market_snapshots ms ON ms.id = c.market_snapshot_id
        WHERE {' AND '.join(filters)}
        ORDER BY d.created_at DESC, d.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _entry_decision_ref_payload(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    diagnostics = _json_object(_row_value(row, "diagnostics_json"))
    context_id = _row_value(row, "scan_context_id")
    if context_id is not None:
        snapshot_id = _row_value(row, "id")
        if _row_value(row, "scan_context_joined_id") is None:
            raise RuntimeError(
                f"decision snapshot {snapshot_id} references missing scan context {context_id}"
            )
        required = {
            "schema_version": _row_value(row, "scan_context_schema_version"),
            "created_at": _row_value(row, "scan_context_created_at"),
            "target_date": _row_value(row, "scan_context_target_date"),
            "prediction_features_json": _row_value(
                row, "scan_context_prediction_features_json"
            ),
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise RuntimeError(
                f"decision snapshot {snapshot_id} references partial scan context "
                f"{context_id}; missing {', '.join(missing)}"
            )
        schema_version = required["schema_version"]
        if schema_version != 1:
            raise RuntimeError(
                "decision snapshot "
                f"{snapshot_id} has unsupported scan context schema_version "
                f"{schema_version!r} (context {context_id})"
            )
        if str(required["target_date"]) != str(_row_value(row, "target_date")):
            raise RuntimeError(
                f"decision snapshot {snapshot_id} target_date does not match "
                f"scan context {context_id}"
            )
    if (
        diagnostics.get("kind") == "trade_decision_signal"
        and context_id is not None
    ):
        markets = _json_object(_row_value(row, "scan_context_market_json"))
        market = markets.get(str(_row_value(row, "market_ticker")))
        if market is None:
            market = _market_diagnostics_from_snapshot_json(
                _row_value(row, "scan_context_market_snapshot_json"),
                str(_row_value(row, "market_ticker") or ""),
            )
        diagnostics = _drop_none(
            {
                "schema_version": 1,
                "kind": "trade_decision",
                "created_at": _row_value(row, "scan_context_created_at", _row_value(row, "created_at")),
                "target_date": _row_value(row, "scan_context_target_date", _row_value(row, "target_date")),
                "risk_profile": _row_value(row, "scan_context_risk_profile", _row_value(row, "risk_profile")),
                "bankroll": _round_number(_row_value(row, "scan_context_bankroll")),
                "context_refs": {
                    "forecast_snapshot_id": _row_value(
                        row,
                        "scan_context_forecast_snapshot_id",
                        _row_value(row, "forecast_snapshot_id"),
                    ),
                    "market_snapshot_id": _row_value(
                        row,
                        "scan_context_market_snapshot_id",
                        _row_value(row, "market_snapshot_id"),
                    ),
                },
                "signal": diagnostics.get("signal", {}),
                "forecast": _json_optional_object(
                    _row_value(row, "scan_context_forecast_json")
                ),
                "intraday": _json_optional_object(
                    _row_value(row, "scan_context_intraday_json")
                ),
                "market": market,
                "market_consensus": _json_optional_object(
                    _row_value(row, "scan_context_market_consensus_json")
                ),
                "prediction_features": _json_object(
                    _row_value(row, "scan_context_prediction_features_json")
                ),
                "strategy_config": _json_optional_object(
                    _row_value(row, "scan_context_strategy_config_json")
                ),
            }
        )
    if not diagnostics:
        diagnostics = {
            "schema_version": 1,
            "kind": "legacy_trade_decision",
            "signal": {
                "approved": bool(_row_value(row, "approved", 0)),
                "signal_approved": bool(
                    _row_value(row, "signal_approved", _row_value(row, "approved", 0))
                ),
                "entry_block_reason": _row_value(row, "entry_block_reason"),
                "probability": _round_number(_row_value(row, "probability")),
                "edge": _round_number(_row_value(row, "edge")),
                "edge_lcb": _round_number(_row_value(row, "edge_lcb")),
                "reasons": _json_list(_row_value(row, "reasons_json")),
            },
        }
    return _drop_none(
        {
            "snapshot_id": int(_row_value(row, "id")),
            "created_at": _row_value(row, "created_at"),
            "approved": bool(_row_value(row, "approved", 0)),
            "signal_approved": bool(
                _row_value(row, "signal_approved", _row_value(row, "approved", 0))
            ),
            "entry_block_reason": _row_value(row, "entry_block_reason"),
            "diagnostics": diagnostics,
        }
    )


def _order_entry_snapshot(row: sqlite3.Row) -> dict[str, object]:
    return _drop_none(
        {
            "entry_decision_snapshot_id": _row_value(row, "entry_decision_snapshot_id"),
            "created_at": _row_value(row, "created_at"),
            "entry_price": _round_number(_row_value(row, "entry_price")),
            "cost_per_contract": _round_number(_row_value(row, "cost_per_contract")),
            "contracts": _round_number(_row_value(row, "contracts")),
            "probability": _round_number(_row_value(row, "probability")),
            "edge": _round_number(_row_value(row, "edge")),
            "edge_lcb": _round_number(_row_value(row, "edge_lcb")),
            "reasons": _json_list(_row_value(row, "reasons_json")),
        }
    )


def _win_loss_reason(
    event: str,
    *,
    side: str,
    resolved_yes: bool | None,
    position_won: bool | None,
    realized_pnl: float,
) -> str:
    if event == "expiration":
        return "Limit order expired unfilled at settlement."
    if event == "close":
        if position_won is True:
            return f"{side} position won because it was closed for positive PnL before settlement."
        if position_won is False:
            return f"{side} position lost because it was closed for negative PnL before settlement."
        return "Position was closed at break-even before settlement."
    if resolved_yes is None or position_won is None:
        return "Outcome was recorded without a resolved market side."
    market_result = "YES" if resolved_yes else "NO"
    verb = "won" if position_won else "lost"
    return f"{side} position {verb} because the market resolved {market_result}."


def _json_text(value: object) -> str | None:
    return json.dumps(value, sort_keys=True) if value is not None else None


def _json_optional_object(value: object) -> dict[str, object] | None:
    return _json_object(value) if value else None
