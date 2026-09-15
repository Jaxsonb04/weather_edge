"""Fixed identities and risk policy for isolated paper-research sleeves."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time, timezone
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


# REG-1 (2026-09-07) moved the research lead measure -- and therefore the
# ``lead_bucket`` label stamped on every research decision and order -- from the
# Los Angeles civil day onto the station's fixed-standard settlement day. The
# two clocks name the same date except between 05:00 and 08:00 UTC, so a label
# written inside that window can mean different things before and after the
# change and a label written outside it cannot. Both directions occur: at
# 05:00-07:00 UTC an Eastern/Central station is a day ahead of Los Angeles (a
# row labelled "day-ahead" was same-day at its station), and at 07:00-08:00 UTC
# during DST a Pacific-standard station is a day behind (a row labelled
# "same-day" was day-ahead at its station).
#
# Historical rows are NOT backfilled. Consumers that group by ``lead_bucket``
# across the change must therefore report how many of their rows fall in this
# window rather than assume one definition -- research_goals._lead_split does.
# The window is a property of the registry's station offsets (twenty cities
# since 2026-09-13), not a guess: it is
# re-derived for every hour of a full year by
# test_lead_bucket_clock_window_covers_every_station_disagreement.
LEAD_BUCKET_CLOCK_AMBIGUOUS_UTC_HOURS = frozenset({5, 6, 7})


def lead_bucket_clock_is_ambiguous(created_at: datetime) -> bool:
    """Whether a row stamped at this moment has a clock-dependent lead label."""

    if created_at.tzinfo is None:
        raise ValueError("lead bucket clock check requires an aware timestamp")
    hour = created_at.astimezone(timezone.utc).hour
    return hour in LEAD_BUCKET_CLOCK_AMBIGUOUS_UTC_HOURS


# Research scan order (2026-09-13). cmd_portfolio_scan walks cities in
# cities.CITIES registry order (mia, lax, chi, atl, ...). The research book
# shares one daily budget and aggregate/region caps across cities, so the first
# cities scanned take the room: 8/31-9/12 entered cost followed the registry
# order (MIA scanned 1st: 14 positions/$439; ATL 4th: 13/$232, -$84.87) while
# LAX got one position for $4.77. Public tape, every registry series measured
# the same way (events 2026-09-05..13; day-ahead taker-YES = NO-seller
# contracts per event day): LAX 14,132; NYC 4,482; MIA 3,122; AUS 2,686; CHI
# 2,476; DAL 1,943; SFO 1,794; OKC 1,753; DEN 1,638; LV 1,530; ATL 1,427; PHL
# 1,336; BOS 1,313; SEA 1,217; HOU 1,005; PHX 978; SATX 786; MIN 758; NOLA 721;
# DC 680. LAX alone is 31% of the twenty-series total. Fourteen series were
# measured on 2026-09-13; SEA, DAL, OKC, SFO, HOU and ATL were measured at
# integration on 2026-09-14 (re-measuring PHX and DC reproduced the 09-13
# counts exactly). All six measured above PHX, which the first version of this
# list had assumed they trailed, and DAL, SFO and OKC also measured above DEN.
# The five 2026-09-13 registry additions (LV, MIN, SATX, NOLA, DC) rank by the
# same measurement. Research-only: the live profile keeps registry order, and a
# city missing from this list scans LAST rather than being skipped
# (research_scan_city_rank), so a future city cannot vanish.
RESEARCH_SCAN_CITY_ORDER: tuple[str, ...] = (
    "lax", "nyc", "mia", "aus", "chi", "dal", "sfo", "okc", "den", "lv", "atl",
    "phl", "bos", "sea", "hou", "phx", "satx", "min", "nola", "dc",
)


def research_scan_city_rank(slug: str) -> int:
    """Position of a city in the research scan order; unlisted cities sort last."""

    normalized = str(slug or "").strip().lower()
    try:
        return RESEARCH_SCAN_CITY_ORDER.index(normalized)
    except ValueError:
        return len(RESEARCH_SCAN_CITY_ORDER)


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
#
# September 12 safety overlay: these immutable account/goal identity fields
# remain historical upper bounds. research_entry_risk.py now imposes tighter
# entry sizing and a projected daily-loss reservation without resetting equity
# or rewriting frozen goals. Its execution version is separately fingerprinted.
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
