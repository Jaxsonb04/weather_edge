from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from .cities import CITIES
from .models import TradeDecision


class LiveTradingDisabled(RuntimeError):
    """Raised when a real-money order attempt violates the pilot safety policy."""


DEFAULT_LIVE_RISK_CAPITAL = 1_000.0
DEFAULT_PILOT_MAX_LOSS_PCT = 0.05
DEFAULT_DAILY_LOSS_PCT = 0.02
DEFAULT_PER_TRADE_RISK_PCT = 0.01


@dataclass(frozen=True)
class LiveExecutionPolicy:
    enabled: bool = False
    dry_run: bool = True
    pilot_max_loss: float | None = None
    daily_loss: float | None = None
    per_trade_risk: float | None = None
    risk_capital: float = DEFAULT_LIVE_RISK_CAPITAL
    pilot_max_loss_pct: float = DEFAULT_PILOT_MAX_LOSS_PCT
    daily_loss_pct: float = DEFAULT_DAILY_LOSS_PCT
    per_trade_risk_pct: float = DEFAULT_PER_TRADE_RISK_PCT

    def __post_init__(self) -> None:
        for name in ("enabled", "dry_run"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")

        def _number(name: str, value: object) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
            return normalized

        for name in (
            "risk_capital",
            "pilot_max_loss_pct",
            "daily_loss_pct",
            "per_trade_risk_pct",
        ):
            value = _number(name, getattr(self, name))
            if name.endswith("_pct") and value > 1.0:
                raise ValueError(f"{name} must be no greater than 1")
            object.__setattr__(self, name, value)

        for cap_name, pct_name in (
            ("pilot_max_loss", "pilot_max_loss_pct"),
            ("daily_loss", "daily_loss_pct"),
            ("per_trade_risk", "per_trade_risk_pct"),
        ):
            if getattr(self, cap_name) is None:
                object.__setattr__(
                    self,
                    cap_name,
                    float(self.risk_capital) * float(getattr(self, pct_name)),
                )

        for name in ("pilot_max_loss", "daily_loss", "per_trade_risk"):
            object.__setattr__(self, name, _number(name, getattr(self, name)))

        if not (
            float(self.per_trade_risk)  # type: ignore[arg-type]
            <= float(self.daily_loss)  # type: ignore[arg-type]
            <= float(self.pilot_max_loss)  # type: ignore[arg-type]
            <= float(self.risk_capital)
        ):
            raise ValueError(
                "risk cap hierarchy must satisfy "
                "per_trade_risk <= daily_loss <= pilot_max_loss <= risk_capital"
            )

    @classmethod
    def from_env(cls) -> "LiveExecutionPolicy":
        def _optional_float(name: str) -> float | None:
            value = os.getenv(name)
            if value is None:
                return None
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
            try:
                return float(value)
            except ValueError as exc:
                raise ValueError(f"{name} must be numeric") from exc

        return cls(
            enabled=os.getenv("SFO_LIVE_TRADING_ENABLED", "0") == "1",
            dry_run=os.getenv("SFO_LIVE_TRADING_DRY_RUN", "1") != "0",
            risk_capital=float(
                os.getenv("SFO_LIVE_RISK_CAPITAL", str(DEFAULT_LIVE_RISK_CAPITAL))
            ),
            pilot_max_loss_pct=float(
                os.getenv(
                    "SFO_LIVE_PILOT_MAX_LOSS_PCT",
                    str(DEFAULT_PILOT_MAX_LOSS_PCT),
                )
            ),
            daily_loss_pct=float(
                os.getenv("SFO_LIVE_DAILY_LOSS_PCT", str(DEFAULT_DAILY_LOSS_PCT))
            ),
            per_trade_risk_pct=float(
                os.getenv(
                    "SFO_LIVE_PER_TRADE_RISK_PCT",
                    str(DEFAULT_PER_TRADE_RISK_PCT),
                )
            ),
            pilot_max_loss=_optional_float("SFO_LIVE_PILOT_MAX_LOSS"),
            daily_loss=_optional_float("SFO_LIVE_DAILY_LOSS"),
            per_trade_risk=_optional_float("SFO_LIVE_PER_TRADE_RISK"),
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

    @staticmethod
    def _intent_risk(decision: TradeDecision) -> float:
        side = str(decision.side).upper()
        ticker = str(decision.ticker)
        try:
            contracts = float(decision.recommended_contracts)
            cost = float(decision.cost_per_contract)
        except (TypeError, ValueError) as exc:
            raise LiveTradingDisabled("invalid live order intent") from exc
        ticker_allowed = any(
            ticker.startswith(f"{city.series_ticker}-") for city in CITIES
        )
        valid = (
            decision.approved is True
            and not decision.entry_block_reason
            and ticker_allowed
            and side in {"YES", "NO"}
            and decision.action == f"BUY_{side}"
            and math.isfinite(contracts)
            and contracts > 0.0
            and contracts.is_integer()
            and math.isfinite(cost)
            and 0.0 < cost <= 1.0
        )
        if not valid:
            raise LiveTradingDisabled("invalid live order intent")
        risk = contracts * cost
        if not math.isfinite(risk):
            raise LiveTradingDisabled("invalid live order intent")
        return risk

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
        if not math.isfinite(float(readiness.realized_pilot_pnl)) or not math.isfinite(
            float(daily_realized_pnl)
        ):
            raise LiveTradingDisabled("invalid live risk state")
        if readiness.realized_pilot_pnl <= -self.policy.pilot_max_loss:
            raise LiveTradingDisabled("pilot loss cap reached")
        if daily_realized_pnl <= -self.policy.daily_loss:
            raise LiveTradingDisabled("daily live loss cap reached")
        if not data_fresh:
            raise LiveTradingDisabled("stale forecast or market data")
        if not decisions:
            raise LiveTradingDisabled("invalid live order intent: empty batch")
        intent_risks = [self._intent_risk(decision) for decision in decisions]
        for spend in intent_risks:
            if spend > self.policy.per_trade_risk + 1e-9:
                raise LiveTradingDisabled("per-trade live risk cap exceeded")
        remaining_daily_risk = self.policy.daily_loss + min(float(daily_realized_pnl), 0.0)
        remaining_pilot_risk = self.policy.pilot_max_loss + min(
            float(readiness.realized_pilot_pnl), 0.0
        )
        if sum(intent_risks) > min(remaining_daily_risk, remaining_pilot_risk) + 1e-9:
            raise LiveTradingDisabled("aggregate live risk cap exceeded")
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
