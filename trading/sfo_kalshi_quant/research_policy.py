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
    allocator_version: str | None = None

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
        # Keep the frozen v1 payload byte-for-byte stable while letting later
        # policies identify allocator semantics that percentages alone cannot.
        if self.allocator_version is not None:
            payload["allocator_version"] = self.allocator_version
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]


RESEARCH_OBJECTIVE_TZ = ZoneInfo("America/Los_Angeles")
RESEARCH_OBJECTIVE_ROLLOVER = time(0, 0)


def canonical_research_lead_bucket(lead_days: int) -> str:
    """Return the one audit label used for a non-past research horizon."""

    if isinstance(lead_days, bool) or not isinstance(lead_days, int) or lead_days < 0:
        raise ValueError("research lead days must be a non-negative integer")
    return "same-day" if lead_days == 0 else "day-ahead"


TARGET_POLICY_V1 = ResearchSleevePolicy(
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


# Frozen paper-only growth experiment. Its ledger remains an independent
# historical control after the v3 account cutover.
TARGET_POLICY_V2 = ResearchSleevePolicy(
    sleeve=ResearchSleeve.TARGET,
    account_id="paper-research-target-v2",
    policy_version="research-target-growth-v2",
    reference_equity=1000.0,
    target_return=0.016,
    max_position_risk_pct=0.06,
    max_city_target_risk_pct=0.06,
    max_region_day_risk_pct=0.12,
    max_aggregate_risk_pct=0.25,
    daily_loss_pause_pct=0.10,
    min_lead_days=1,
    one_contract=False,
    allocator_version="policy-sized-v2",
)

# Frozen paper-only ROI experiment (2026-07-26..29). Measured outcome: the
# larger per-position budget (8%) turned every maker request into ~90 contracts
# whose reservation consumed the city/region/aggregate caps, collapsing the
# number of concurrently resting quotes from ~9.6 to ~4.3 and realized P&L from
# ~$8/day (v1 geometry) to ~$1/day. Day-clustered attribution of the position
# knob alone: $2.70/day (95% CI $0.30-$6.15). Its ledger stays archived as the
# evidence record of that measurement.
TARGET_POLICY_V3 = ResearchSleevePolicy(
    sleeve=ResearchSleeve.TARGET,
    account_id="paper-research-roi-v3",
    policy_version="research-target-roi-v3",
    reference_equity=1000.0,
    target_return=0.05,
    max_position_risk_pct=0.08,
    max_city_target_risk_pct=0.10,
    max_region_day_risk_pct=0.20,
    max_aggregate_risk_pct=0.40,
    daily_loss_pause_pct=0.12,
    min_lead_days=1,
    one_contract=False,
    allocator_version="policy-sized-v3",
)

# Frozen breadth-restoration era (2026-07-29..31). v4 restored the v1 risk
# geometry after the v3 oversize experiment starved quote breadth; its first
# full day realized +$30.17 on 272 filled contracts. Its ledger stays archived
# as the evidence record that breadth, not per-order size, is the volume lever.
TARGET_POLICY_V4 = ResearchSleevePolicy(
    sleeve=ResearchSleeve.TARGET,
    account_id="paper-research-roi-v4",
    policy_version="research-target-roi-v4",
    reference_equity=1000.0,
    target_return=0.05,
    max_position_risk_pct=0.03,
    max_city_target_risk_pct=0.06,
    max_region_day_risk_pct=0.12,
    max_aggregate_risk_pct=0.25,
    daily_loss_pause_pct=0.10,
    min_lead_days=1,
    one_contract=False,
    allocator_version="policy-sized-v3",
)

# Frozen 1.5x step (2026-07-31). Superseded by v6 the same day once the size
# curve was measured end-to-end; kept as an archived ledger.
# v5 scaled the v4 geometry by exactly 1.5x
# on every dollar knob while PRESERVING the ratios that make breadth work
# (aggregate/position stays ~8.3 concurrent quotes, city/position stays 2 per
# city). Evidence for the size step: every one of v4's day-one winners filled
# its FULL request (fills were request-truncated, 40-53 contracts at 0.56-0.72
# limits), and 19/59 of the v1 era's fills were request-truncated too - on
# burst days the tape absorbs more than the $30 budget bought. v3's failure
# was raising size while holding the shared caps fixed, which traded breadth
# for size; v5 raises both together so concurrency is unchanged.
TARGET_POLICY_V5 = ResearchSleevePolicy(
    sleeve=ResearchSleeve.TARGET,
    account_id="paper-research-roi-v5",
    policy_version="research-target-roi-v5",
    reference_equity=1000.0,
    target_return=0.05,
    max_position_risk_pct=0.045,
    max_city_target_risk_pct=0.09,
    max_region_day_risk_pct=0.18,
    max_aggregate_risk_pct=0.375,
    daily_loss_pause_pct=0.12,
    min_lead_days=1,
    one_contract=False,
    allocator_version="policy-sized-v3",
)


# Active paper-only ROI experiment. v6 is the LAST uniform size step: the size
# curve was replayed end-to-end against the recorded public tape (every maker
# parent order re-run through the repo's own allocate_maker_fills and the db.py
# capacity gate), and it saturates. Measured $/day by scale factor over v4:
# 1.5x (v5) baseline, 3.0x $15.2, 3.5x $15.5, 4.0x $15.8, 6.0x $16.4 - and both
# sub-steps above 3.0x have bootstrap 95% lower bounds of exactly $0.00. The
# 1.5x -> 3.0x step is +$2.9/day (day-clustered 95% CI [+$0.62, +$5.73]) and
# survives leave-one-day-out ([+$1.98, +$3.21]) and leave-one-ORDER-out; the
# 3.0x -> 6.0x step does not (it is one order, and deleting it flips the sign
# negative). Request-truncation - the signal that size is still binding - is
# already spent at 3.0x (3.9% -> 2.6%).
#
# STOP RULE: do NOT propose a further uniform size step on tape evidence alone.
# The gate for any v7 is a NEWLY MEASURED truncation rate above ~10% on filled
# maker orders after v6 has run, not an opinion. Above 3.0x the extra request
# size is dead-weight reservation: capture of requested size falls 2.21% (3.0x)
# -> 1.75% (4.0x) -> 1.00% (8x), while the structural full-loss day grows from
# -$237 to -$327.
#
# GEOMETRY INVARIANT (enforced by test_target_geometry_invariants): the ratios,
# not the absolute dollars, are what fund breadth. v3 raised size while holding
# the shared caps fixed, cutting concurrent resting quotes 9.6 -> 4.3 and
# costing $2.70/day. Every scale step must move ALL dollar knobs together so
# aggregate/position stays 8.33 and city/position stays 2.0.
#
# target_return stays 0.05. The daily target-attained lock (db.py: "target
# attained: new target risk is locked for the objective day") only halts new
# entries once a day has already realized $50 - more than double the book's
# best day to date - and it was measured blocking 0 of 487 placements. It is a
# real ceiling on compounding at this size, but raising it is a change to the
# published KPI and to the goal-freezing contract, so it waits for the day the
# lock is measured actually firing rather than being bundled into a size step.
TARGET_POLICY = ResearchSleevePolicy(
    sleeve=ResearchSleeve.TARGET,
    account_id="paper-research-roi-v6",
    policy_version="research-target-roi-v6",
    reference_equity=1000.0,
    target_return=0.05,
    max_position_risk_pct=0.09,
    max_city_target_risk_pct=0.18,
    max_region_day_risk_pct=0.36,
    max_aggregate_risk_pct=0.75,
    daily_loss_pause_pct=0.15,
    min_lead_days=1,
    one_contract=False,
    allocator_version="policy-sized-v3",
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


ALL_RESEARCH_POLICIES = (
    TARGET_POLICY_V1,
    TARGET_POLICY_V2,
    TARGET_POLICY_V3,
    TARGET_POLICY_V4,
    TARGET_POLICY_V5,
    TARGET_POLICY,
    MOTION_POLICY,
)
