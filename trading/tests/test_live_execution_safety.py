from __future__ import annotations

import pytest

from sfo_kalshi_quant.live_execution import (
    LiveExecutionPolicy,
    LiveTradingDisabled,
    LiveTradingReadiness,
    RealOrderAdapter,
    readiness_status_from_checks,
)
from sfo_kalshi_quant.models import TradeDecision


def _decision(*, spend: float = 8.0) -> TradeDecision:
    contracts = spend / 0.80
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B72.5",
        label="72° to 73°",
        action="BUY_NO",
        approved=True,
        probability=0.95,
        probability_lcb=0.90,
        yes_bid=0.18,
        yes_ask=0.20,
        spread=0.02,
        fee_per_contract=0.0,
        cost_per_contract=0.80,
        edge=0.15,
        edge_lcb=0.10,
        kelly_fraction=0.01,
        recommended_contracts=contracts,
        expected_profit=contracts * 0.15,
        reasons=[],
        side="NO",
        entry_bid=0.78,
        entry_ask=0.80,
        entry_bid_size=50.0,
        entry_ask_size=50.0,
    )


def test_real_order_adapter_refuses_orders_by_default() -> None:
    adapter = RealOrderAdapter(policy=LiveExecutionPolicy())

    with pytest.raises(LiveTradingDisabled, match="disabled"):
        adapter.place_orders([_decision()], readiness=LiveTradingReadiness(status="PILOT_READY"))


def test_real_order_adapter_enforces_readiness_and_pilot_loss_caps() -> None:
    policy = LiveExecutionPolicy(enabled=True, dry_run=False, pilot_max_loss=50.0, per_trade_risk=10.0)
    adapter = RealOrderAdapter(policy=policy)

    with pytest.raises(LiveTradingDisabled, match="readiness"):
        adapter.place_orders([_decision()], readiness=LiveTradingReadiness(status="PAPER_READY"))

    with pytest.raises(LiveTradingDisabled, match="pilot loss"):
        adapter.place_orders(
            [_decision()],
            readiness=LiveTradingReadiness(status="PILOT_READY", realized_pilot_pnl=-50.0),
        )

    with pytest.raises(LiveTradingDisabled, match="per-trade"):
        adapter.place_orders(
            [_decision(spend=12.0)],
            readiness=LiveTradingReadiness(status="PILOT_READY"),
        )


def test_real_order_adapter_dry_run_returns_intents_without_live_order_side_effects() -> None:
    adapter = RealOrderAdapter(policy=LiveExecutionPolicy(enabled=True, dry_run=True))

    intents = adapter.place_orders([_decision()], readiness=LiveTradingReadiness(status="PILOT_READY"))

    assert len(intents) == 1
    assert intents[0]["mode"] == "dry_run"
    assert intents[0]["ticker"] == "KXHIGHTSFO-TEST-B72.5"


def test_real_order_adapter_blocks_daily_loss_cap_and_stale_data() -> None:
    adapter = RealOrderAdapter(policy=LiveExecutionPolicy(enabled=True, dry_run=True))
    readiness = LiveTradingReadiness(status="PILOT_READY")

    with pytest.raises(LiveTradingDisabled, match="daily live loss cap"):
        adapter.place_orders([_decision()], readiness=readiness, daily_realized_pnl=-20.0)

    with pytest.raises(LiveTradingDisabled, match="stale"):
        adapter.place_orders([_decision()], readiness=readiness, data_fresh=False)


def test_readiness_status_mapping_surfaces_pilot_ready_and_paused() -> None:
    assert (
        readiness_status_from_checks(
            evidence_passed=True,
            software_passed=True,
            paper_ready=True,
            pilot_loss_remaining=50.0,
        ).status
        == "PILOT_READY"
    )
    assert (
        readiness_status_from_checks(
            evidence_passed=True,
            software_passed=True,
            paper_ready=True,
            pilot_loss_remaining=0.0,
        ).status
        == "PILOT_PAUSED"
    )
    not_ready = readiness_status_from_checks(
        evidence_passed=False,
        software_passed=True,
        paper_ready=False,
        pilot_loss_remaining=50.0,
    )
    assert not_ready.status == "NOT_READY"
    assert not_ready.failing_checks
