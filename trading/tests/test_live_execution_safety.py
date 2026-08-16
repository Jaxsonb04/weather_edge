from __future__ import annotations

from dataclasses import replace

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


def test_live_execution_policy_derives_caps_from_configured_risk_capital(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "SFO_LIVE_PILOT_MAX_LOSS",
        "SFO_LIVE_DAILY_LOSS",
        "SFO_LIVE_PER_TRADE_RISK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SFO_LIVE_RISK_CAPITAL", "2500")
    monkeypatch.setenv("SFO_LIVE_PILOT_MAX_LOSS_PCT", "0.05")
    monkeypatch.setenv("SFO_LIVE_DAILY_LOSS_PCT", "0.02")
    monkeypatch.setenv("SFO_LIVE_PER_TRADE_RISK_PCT", "0.01")

    policy = LiveExecutionPolicy.from_env()

    assert policy.risk_capital == pytest.approx(2500.0)
    assert policy.pilot_max_loss == pytest.approx(125.0)
    assert policy.daily_loss == pytest.approx(50.0)
    assert policy.per_trade_risk == pytest.approx(25.0)


@pytest.mark.parametrize(
    "name",
    ("pilot_max_loss_pct", "daily_loss_pct", "per_trade_risk_pct"),
)
def test_live_execution_policy_rejects_percentages_above_full_capital(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="no greater than 1"):
        LiveExecutionPolicy(**{name: 1.01})


@pytest.mark.parametrize(
    "overrides",
    (
        {"risk_capital": 100.0, "pilot_max_loss": 101.0},
        {"pilot_max_loss": 50.0, "daily_loss": 51.0},
        {"daily_loss": 20.0, "per_trade_risk": 21.0},
    ),
)
def test_live_execution_policy_rejects_inverted_risk_cap_hierarchy(
    overrides: dict[str, float],
) -> None:
    with pytest.raises(ValueError, match="risk_capital"):
        LiveExecutionPolicy(**overrides)


def test_live_execution_policy_keeps_explicit_dollar_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SFO_LIVE_RISK_CAPITAL", "2500")
    monkeypatch.setenv("SFO_LIVE_PILOT_MAX_LOSS_PCT", "0.05")
    monkeypatch.setenv("SFO_LIVE_DAILY_LOSS_PCT", "0.02")
    monkeypatch.setenv("SFO_LIVE_PER_TRADE_RISK_PCT", "0.01")
    monkeypatch.setenv("SFO_LIVE_PILOT_MAX_LOSS", "75")
    monkeypatch.setenv("SFO_LIVE_DAILY_LOSS", "30")
    monkeypatch.setenv("SFO_LIVE_PER_TRADE_RISK", "12")

    policy = LiveExecutionPolicy.from_env()

    assert policy.pilot_max_loss == pytest.approx(75.0)
    assert policy.daily_loss == pytest.approx(30.0)
    assert policy.per_trade_risk == pytest.approx(12.0)


def test_live_execution_policy_preserves_legacy_positional_cap_order() -> None:
    policy = LiveExecutionPolicy(True, False, 50.0, 20.0, 10.0)

    assert policy.enabled is True
    assert policy.dry_run is False
    assert policy.pilot_max_loss == pytest.approx(50.0)
    assert policy.daily_loss == pytest.approx(20.0)
    assert policy.per_trade_risk == pytest.approx(10.0)


@pytest.mark.parametrize(
    "name",
    (
        "SFO_LIVE_PILOT_MAX_LOSS",
        "SFO_LIVE_DAILY_LOSS",
        "SFO_LIVE_PER_TRADE_RISK",
    ),
)
@pytest.mark.parametrize("value", ["", "   "])
def test_live_execution_policy_rejects_blank_legacy_dollar_overrides(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    for legacy_name in (
        "SFO_LIVE_PILOT_MAX_LOSS",
        "SFO_LIVE_DAILY_LOSS",
        "SFO_LIVE_PER_TRADE_RISK",
    ):
        monkeypatch.delenv(legacy_name, raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        LiveExecutionPolicy.from_env()


@pytest.mark.parametrize("value", ["10", True, False])
def test_live_execution_policy_rejects_non_numeric_direct_cap_values(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="number"):
        LiveExecutionPolicy(per_trade_risk=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["enabled", "dry_run"])
def test_live_execution_policy_requires_boolean_mode_gates(name: str) -> None:
    with pytest.raises(ValueError, match="boolean"):
        LiveExecutionPolicy(**{name: 1})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, -1.0])
def test_live_execution_policy_rejects_nonpositive_or_nonfinite_caps(value: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        LiveExecutionPolicy(per_trade_risk=value)


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


@pytest.mark.parametrize(
    "decision",
    [
        replace(_decision(), approved=False),
        replace(_decision(), ticker="NOT-A-REAL-MARKET"),
        replace(_decision(), side="INVALID"),
        replace(_decision(), action="BUY_YES"),
        replace(_decision(), recommended_contracts=float("nan")),
        replace(_decision(), recommended_contracts=1.5),
        replace(_decision(), cost_per_contract=-0.10),
    ],
)
def test_real_order_adapter_rejects_invalid_order_intents(decision: TradeDecision) -> None:
    adapter = RealOrderAdapter(policy=LiveExecutionPolicy(enabled=True, dry_run=True))

    with pytest.raises(LiveTradingDisabled, match="invalid live order intent"):
        adapter.place_orders(
            [decision],
            readiness=LiveTradingReadiness(status="PILOT_READY"),
        )


def test_real_order_adapter_dry_run_returns_intents_without_live_order_side_effects() -> None:
    adapter = RealOrderAdapter(policy=LiveExecutionPolicy(enabled=True, dry_run=True))

    intents = adapter.place_orders([_decision()], readiness=LiveTradingReadiness(status="PILOT_READY"))

    assert len(intents) == 1
    assert intents[0]["mode"] == "dry_run"
    assert intents[0]["ticker"] == "KXHIGHTSFO-TEST-B72.5"


def test_valid_non_dry_request_remains_blocked_without_authenticated_client() -> None:
    adapter = RealOrderAdapter(
        policy=LiveExecutionPolicy(enabled=True, dry_run=False)
    )

    with pytest.raises(
        LiveTradingDisabled,
        match="authenticated live order client is not implemented",
    ):
        adapter.place_orders(
            [_decision()],
            readiness=LiveTradingReadiness(status="PILOT_READY"),
        )


def test_real_order_adapter_caps_aggregate_new_risk_by_remaining_daily_loss_budget() -> None:
    adapter = RealOrderAdapter(
        policy=LiveExecutionPolicy(
            enabled=True,
            dry_run=True,
            per_trade_risk=10.0,
            daily_loss=20.0,
        )
    )
    readiness = LiveTradingReadiness(status="PILOT_READY")

    with pytest.raises(LiveTradingDisabled, match="aggregate live risk cap"):
        adapter.place_orders(
            [_decision(spend=8.0), _decision(spend=8.0), _decision(spend=8.0)],
            readiness=readiness,
        )

    with pytest.raises(LiveTradingDisabled, match="aggregate live risk cap"):
        adapter.place_orders(
            [_decision(spend=8.0)],
            readiness=readiness,
            daily_realized_pnl=-15.0,
        )


def test_real_order_adapter_caps_aggregate_new_risk_by_remaining_pilot_loss_budget() -> None:
    adapter = RealOrderAdapter(
        policy=LiveExecutionPolicy(
            enabled=True,
            dry_run=True,
            pilot_max_loss=50.0,
            daily_loss=20.0,
            per_trade_risk=10.0,
        )
    )
    readiness = LiveTradingReadiness(
        status="PILOT_READY",
        realized_pilot_pnl=-45.0,
    )

    # The $8 intent is below both the $10 per-order cap and the untouched $20
    # daily budget, but exceeds the $5 remaining pilot-loss budget.
    with pytest.raises(LiveTradingDisabled, match="aggregate live risk cap"):
        adapter.place_orders(
            [_decision(spend=8.0)],
            readiness=readiness,
            daily_realized_pnl=0.0,
        )


def test_real_order_adapter_rejects_an_empty_batch() -> None:
    adapter = RealOrderAdapter(policy=LiveExecutionPolicy(enabled=True, dry_run=True))

    with pytest.raises(LiveTradingDisabled, match="invalid live order intent"):
        adapter.place_orders(
            [],
            readiness=LiveTradingReadiness(status="PILOT_READY"),
        )


def test_real_order_adapter_rejects_nonfinite_risk_state() -> None:
    adapter = RealOrderAdapter(policy=LiveExecutionPolicy(enabled=True, dry_run=True))

    with pytest.raises(LiveTradingDisabled, match="invalid live risk state"):
        adapter.place_orders(
            [_decision()],
            readiness=LiveTradingReadiness(
                status="PILOT_READY",
                realized_pilot_pnl=float("nan"),
            ),
        )

    with pytest.raises(LiveTradingDisabled, match="invalid live risk state"):
        adapter.place_orders(
            [_decision()],
            readiness=LiveTradingReadiness(status="PILOT_READY"),
            daily_realized_pnl=float("nan"),
        )


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
