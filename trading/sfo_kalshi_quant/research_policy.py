"""Fixed identities and risk policy for isolated paper-research sleeves."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import time
from enum import Enum
from zoneinfo import ZoneInfo


class ResearchSleeve(str, Enum):
    """Purpose-specific research books that never share account state."""

    TARGET = "target"
    MOTION = "motion"


@dataclass(frozen=True)
class ResearchSleevePolicy:
    """Immutable, auditable controls for one paper-research account."""

    sleeve: ResearchSleeve
    account_id: str
    policy_version: str
    reference_equity: float
    target_return: float
    max_position_risk_pct: float
    max_city_target_risk_pct: float
    max_region_day_risk_pct: float
    max_aggregate_risk_pct: float
    daily_loss_pause_pct: float
    min_lead_days: int
    one_contract: bool

    @property
    def target_pnl(self) -> float:
        return self.reference_equity * self.target_return

    @property
    def policy_fingerprint(self) -> str:
        """Return the stable identity of every execution-relevant policy field."""

        payload = {
            "account_id": self.account_id,
            "daily_loss_pause_pct": self.daily_loss_pause_pct,
            "max_aggregate_risk_pct": self.max_aggregate_risk_pct,
            "max_city_target_risk_pct": self.max_city_target_risk_pct,
            "max_position_risk_pct": self.max_position_risk_pct,
            "max_region_day_risk_pct": self.max_region_day_risk_pct,
            "min_lead_days": self.min_lead_days,
            "one_contract": self.one_contract,
            "policy_version": self.policy_version,
            "reference_equity": self.reference_equity,
            "sleeve": self.sleeve.value,
            "target_return": self.target_return,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]


RESEARCH_OBJECTIVE_TZ = ZoneInfo("America/Los_Angeles")
RESEARCH_OBJECTIVE_ROLLOVER = time(0, 0)

TARGET_POLICY = ResearchSleevePolicy(
    sleeve=ResearchSleeve.TARGET,
    account_id="paper-research-target-v1",
    policy_version="research-target-v1",
    reference_equity=1000.0,
    target_return=0.05,
    max_position_risk_pct=0.03,
    max_city_target_risk_pct=0.06,
    max_region_day_risk_pct=0.12,
    max_aggregate_risk_pct=0.25,
    daily_loss_pause_pct=0.10,
    min_lead_days=1,
    one_contract=False,
)

MOTION_POLICY = ResearchSleevePolicy(
    sleeve=ResearchSleeve.MOTION,
    account_id="paper-research-motion-v1",
    policy_version="research-motion-v1",
    reference_equity=1000.0,
    target_return=0.0,
    # One contract, not a percentage, is the binding position limit. Keep this
    # percentage deliberately non-binding so the four documented motion caps
    # retain their exact city/region/aggregate/daily meanings.
    max_position_risk_pct=1.0,
    max_city_target_risk_pct=0.02,
    max_region_day_risk_pct=0.04,
    max_aggregate_risk_pct=0.10,
    daily_loss_pause_pct=0.05,
    min_lead_days=0,
    one_contract=True,
)
