from __future__ import annotations

import os
from dataclasses import dataclass, field

from .models import TradeDecision


class LiveTradingDisabled(RuntimeError):
    """Raised when a real-money order attempt violates the pilot safety policy."""


@dataclass(frozen=True)
class LiveExecutionPolicy:
    enabled: bool = False
    dry_run: bool = True
    pilot_max_loss: float = 50.0
    daily_loss: float = 20.0
    per_trade_risk: float = 10.0

    @classmethod
    def from_env(cls) -> "LiveExecutionPolicy":
        return cls(
            enabled=os.getenv("SFO_LIVE_TRADING_ENABLED", "0") == "1",
            dry_run=os.getenv("SFO_LIVE_TRADING_DRY_RUN", "1") != "0",
            pilot_max_loss=float(os.getenv("SFO_LIVE_PILOT_MAX_LOSS", "50")),
            daily_loss=float(os.getenv("SFO_LIVE_DAILY_LOSS", "20")),
            per_trade_risk=float(os.getenv("SFO_LIVE_PER_TRADE_RISK", "10")),
        )


@dataclass(frozen=True)
class LiveTradingReadiness:
    status: str
    failing_checks: list[str] = field(default_factory=list)
    realized_pilot_pnl: float = 0.0


def readiness_status_from_checks(
    *,
    evidence_passed: bool,
    software_passed: bool,
    paper_ready: bool,
    pilot_loss_remaining: float,
    failing_checks: list[str] | None = None,
) -> LiveTradingReadiness:
    failures = list(failing_checks or [])
    if not evidence_passed:
        failures.append("evidence gate has not passed")
    if not software_passed:
        failures.append("software safety gate has not passed")
    if pilot_loss_remaining <= 0:
        return LiveTradingReadiness(status="PILOT_PAUSED", failing_checks=["pilot loss cap reached"])
    if evidence_passed and software_passed and paper_ready:
        return LiveTradingReadiness(status="PILOT_READY")
    if software_passed and paper_ready:
        return LiveTradingReadiness(status="PAPER_READY", failing_checks=failures)
    return LiveTradingReadiness(status="NOT_READY", failing_checks=failures or ["paper gate has not passed"])


class RealOrderAdapter:
    """Safety wrapper for the future authenticated Kalshi order path.

    This class intentionally does not contain an authenticated client yet. It
    validates all live-money gates and returns dry-run intents unless a future
    implementation explicitly wires a reviewed order client behind this policy.
    """

    def __init__(self, *, policy: LiveExecutionPolicy | None = None) -> None:
        self.policy = policy or LiveExecutionPolicy()

    def place_orders(
        self,
        decisions: list[TradeDecision],
        *,
        readiness: LiveTradingReadiness,
        daily_realized_pnl: float = 0.0,
        data_fresh: bool = True,
    ) -> list[dict[str, object]]:
        if not self.policy.enabled:
            raise LiveTradingDisabled("live trading is disabled")
        if readiness.status != "PILOT_READY":
            raise LiveTradingDisabled(f"live trading blocked by readiness status {readiness.status}")
        if readiness.realized_pilot_pnl <= -self.policy.pilot_max_loss:
            raise LiveTradingDisabled("pilot loss cap reached")
        if daily_realized_pnl <= -self.policy.daily_loss:
            raise LiveTradingDisabled("daily live loss cap reached")
        if not data_fresh:
            raise LiveTradingDisabled("stale forecast or market data")
        for decision in decisions:
            spend = decision.recommended_contracts * decision.cost_per_contract
            if spend > self.policy.per_trade_risk + 1e-9:
                raise LiveTradingDisabled("per-trade live risk cap exceeded")
        if self.policy.dry_run:
            return [
                {
                    "mode": "dry_run",
                    "ticker": decision.ticker,
                    "side": decision.side,
                    "contracts": decision.recommended_contracts,
                    "max_risk": decision.recommended_contracts * decision.cost_per_contract,
                }
                for decision in decisions
            ]
        raise LiveTradingDisabled("authenticated live order client is not implemented")
