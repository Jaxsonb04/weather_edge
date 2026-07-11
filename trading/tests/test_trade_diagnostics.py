from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.consensus import MarketConsensus
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot, TradeDecision

from support import pre_resolution_event


def _decision(
    ticker: str = "KXHIGHTSFO-TEST-B66.5",
    *,
    side: str = "YES",
    approved: bool = True,
) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="66 to 67",
        action="BUY_NO" if side == "NO" else "BUY_YES",
        side=side,
        approved=approved,
        signal_approved=approved,
        probability=0.72,
        probability_lcb=0.64,
        yes_bid=0.24,
        yes_ask=0.26,
        entry_bid=0.24 if side == "YES" else 0.74,
        entry_ask=0.26 if side == "YES" else 0.76,
        entry_bid_size=18,
        entry_ask_size=21,
        spread=0.02,
        fee_per_contract=0.01,
        cost_per_contract=0.27 if side == "YES" else 0.77,
        edge=0.45 if side == "YES" else -0.05,
        edge_lcb=0.37 if side == "YES" else -0.13,
        kelly_fraction=0.04,
        recommended_contracts=3.0,
        expected_profit=1.35,
        trade_quality_score=81.0,
        reasons=["passed core edge gate", "sleeve=core"],
        strike_type="between",
        floor_strike=66.0,
        cap_strike=67.0,
        residual_probability=0.70,
        ensemble_probability=0.74,
        model_probability=0.76,
        market_probability=0.52,
        intraday_probability=0.78,
        remaining_heat_risk=0.08,
        binding_constraint="kelly_budget",
    )


def _forecast() -> ForecastSnapshot:
    return ForecastSnapshot(
        target_date=date(2026, 6, 20),
        predicted_high_f=66.0,
        fetched_at="2026-06-20T12:00:00+00:00",
        lead_hours=8.0,
        method="weatheredge-blend",
        google_high_f=66.5,
        nws_high_f=65.5,
        open_meteo_high_f=66.0,
        history_high_f=64.0,
        google_weight=0.35,
        nws_weight=0.35,
        open_meteo_weight=0.2,
        history_weight=0.1,
        station_adjustment_f=-0.25,
        fresh_station_count=4,
        source_count=4,
        raw={"marine_layer_index": 0.7, "ocean_temp_f": 54.0},
    )


def _intraday() -> IntradaySnapshot:
    return IntradaySnapshot(
        target_date=date(2026, 6, 20),
        observed_high_f=64.0,
        latest_temp_f=63.0,
        latest_observed_at="2026-06-20T19:00:00+00:00",
        remaining_forecast_high_f=66.0,
        forecast_fetched_at="2026-06-20T18:45:00+00:00",
        observation_count=12,
        observed_high_source="meteostat",
        is_complete=False,
    )


def _consensus() -> MarketConsensus:
    return MarketConsensus(
        available=True,
        implied_high_f=65.0,
        modal_bin_ticker="KXHIGHTSFO-TEST-B65.5",
        modal_bin_label="65 to 66",
        modal_probability=0.31,
        implied_stdev_f=2.2,
        p10_f=62.0,
        p25_f=64.0,
        median_f=65.0,
        p75_f=67.0,
        p90_f=69.0,
        overround=0.04,
        liquid_bin_count=7,
        bins=(),
    )


def test_decision_snapshot_persists_normalized_trade_diagnostics() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(min_edge=0.02, max_spread=0.07)
        decision = _decision()
        event = pre_resolution_event([decision])
        event = replace(
            event,
            raw={
                "event_ticker": event.event_ticker,
                "title": event.title,
                "markets": [
                    {
                        **event.markets[0].raw,
                        "ticker": decision.ticker,
                        "event_ticker": event.event_ticker,
                        "yes_sub_title": decision.label,
                        "strike_type": decision.strike_type,
                        "floor_strike": decision.floor_strike,
                        "cap_strike": decision.cap_strike,
                        "yes_bid_dollars": str(decision.yes_bid),
                        "yes_ask_dollars": str(decision.yes_ask),
                        "no_bid_dollars": "0",
                        "no_ask_dollars": "0",
                        "status": "active",
                    }
                ],
            },
        )

        forecast_snapshot_id = store.record_forecast(_forecast())
        market_snapshot_id = store.record_market(event)
        store.record_decisions(
            "2026-06-20",
            [decision],
            forecast=_forecast(),
            intraday=_intraday(),
            event=event,
            market_consensus=_consensus(),
            risk_profile="live",
            bankroll=1234.0,
            strategy_config=config,
            forecast_snapshot_id=forecast_snapshot_id,
            market_snapshot_id=market_snapshot_id,
        )
        order_id = store.record_paper_order(
            "2026-06-20", decision, risk_profile="live", strategy_config=config
        )

        with store.connect() as conn:
            row = conn.execute(
                """
                SELECT d.forecast_snapshot_id, d.market_snapshot_id,
                       d.prediction_features_json, d.diagnostics_json,
                       c.forecast_json, c.intraday_json, c.market_json,
                       c.market_consensus_json,
                       c.prediction_features_json, c.strategy_config_json
                FROM decision_snapshots d
                JOIN scan_context_snapshots c ON c.id = d.scan_context_id
                LIMIT 1
                """
            ).fetchone()
            order_diagnostics = conn.execute(
                "SELECT diagnostics_json FROM paper_orders WHERE id=?", (order_id,)
            ).fetchone()[0]

    payload = json.loads(row[3])
    assert row[0] == forecast_snapshot_id
    assert row[1] == market_snapshot_id
    assert row[2] is None
    assert payload["schema_version"] == 2
    assert payload["signal"]["approved"] is True
    assert payload["signal"]["binding_constraint"] == "kelly_budget"
    assert json.loads(row[4])["source_count"] == 4
    assert json.loads(row[5])["observed_high_f"] == 64.0
    assert row[6] is None  # normalized market_snapshot_id owns the ladder
    assert json.loads(row[7])["implied_high_f"] == 65.0
    assert json.loads(row[8])["marine_layer_index"] == 0.7
    assert json.loads(row[9])["min_edge"] == 0.02
    reconstructed = json.loads(order_diagnostics)["entry_decision"]["diagnostics"]
    assert reconstructed["market"]["ticker"] == decision.ticker


def test_paper_order_inherits_entry_decision_diagnostics() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(min_edge=0.02, max_spread=0.07)
        decision = _decision()
        store.record_decisions(
            "2026-06-20",
            [decision],
            forecast=_forecast(),
            event=pre_resolution_event([decision]),
            risk_profile="live",
            bankroll=1000.0,
            strategy_config=config,
        )
        order_id = store.record_paper_order(
            "2026-06-20",
            decision,
            risk_profile="live",
            strategy_config=config,
        )

        with store.connect() as conn:
            order = conn.execute(
                """
                SELECT entry_decision_snapshot_id, diagnostics_json
                FROM paper_orders
                WHERE id = ?
                """,
                (order_id,),
            ).fetchone()
            decision_id = conn.execute("SELECT id FROM decision_snapshots LIMIT 1").fetchone()[0]

    payload = json.loads(order[1])
    assert order[0] == decision_id
    assert payload["entry_decision"]["snapshot_id"] == decision_id
    assert payload["entry_decision"]["diagnostics"]["signal"]["edge_lcb"] == 0.37
    assert payload["strategy_config"]["max_spread"] == 0.07


def test_settled_order_persists_outcome_diagnostics() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = _decision()
        store.record_decisions(
            "2026-06-20",
            [decision],
            forecast=_forecast(),
            event=pre_resolution_event([decision]),
            risk_profile="research",
            bankroll=1000.0,
            strategy_config=StrategyConfig(),
        )
        order_id = store.record_paper_order(
            "2026-06-20",
            decision,
            risk_profile="research",
            strategy_config=StrategyConfig(),
        )

        assert store.settle_paper_orders("2026-06-20", 67.0) == 1

        row = store.paper_order(order_id)
        payload = json.loads(row["outcome_diagnostics_json"])

    assert payload["outcome"]["event"] == "settlement"
    assert payload["outcome"]["position_won"] is True
    assert payload["outcome"]["resolved_yes"] is True
    assert payload["outcome"]["forecast_error_f"] == 1.0
    assert payload["outcome"]["win_loss_reason"] == "YES position won because the market resolved YES."
    assert payload["entry"]["decision_snapshot_id"] is not None
    assert payload["entry"]["diagnostics"]["prediction_features"]["predicted_high_f"] == 66.0


def test_research_shadow_order_persists_diagnostics_and_entry_decision_link() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = _decision()
        store.record_decisions(
            "2026-06-20",
            [decision],
            forecast=_forecast(),
            event=pre_resolution_event([decision]),
            risk_profile="research",
            bankroll=1000.0,
            strategy_config=StrategyConfig(),
        )
        shadow_id = store.record_research_shadow_order(
            "2026-06-20",
            decision,
            risk_profile="research",
            sample_probability=1.0,
            sampled=False,
            strategy_config=StrategyConfig(),
        )

        with store.connect() as conn:
            row = conn.execute(
                """
                SELECT entry_decision_snapshot_id, diagnostics_json
                FROM research_shadow_orders
                WHERE id = ?
                """,
                (shadow_id,),
            ).fetchone()

    payload = json.loads(row[1])
    assert row[0] is not None
    assert payload["kind"] == "research_shadow_order"
    assert payload["entry_decision"]["diagnostics"]["signal"]["approved"] is True
    assert payload["sampling"]["sample_probability"] == 1.0
