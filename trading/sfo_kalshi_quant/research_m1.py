"""M1 market-recalibration paper trial: frozen policy and pure rule math.

M1 recalibrates the Kalshi ladder's own same-scan de-vigged probability with a
frozen logistic map and buys the side whose recalibrated probability beats the
all-in taker cost by two cents. The weather model never gates an entry; its
agreement is recorded and tested as a pre-registered hypothesis
(``docs/research/m1_preregistration.md``).

This module is deliberately pure: no database, network, clock or environment
access. The scan engine, the daily reconciliation and the stop rules all call
the same functions, so the replay of a recorded evaluation is the rule itself
rather than a second implementation. It never imports ``live_execution``: the
trial is paper-only by construction.

The math mirrors the verified research implementation exactly (the lead-quant
``vlib.py``): the logit clip 0.005/0.995, the bucket by the station's civil
local hour on D-1, the one-contract fee in the 0.02 gate, Kalshi's per-order fee
for sizing and P&L, and the first research-profile scan of each UTC hour as the
evaluation sample.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .fees import quadratic_fee_per_contract, quadratic_fee_total
from .settlement_truth import bin_resolves_yes, integer_settlement_high_f


M1_ACCOUNT_ID = "paper-research-m1-v1"
M1_POLICY_VERSION = "research-m1-market-recal-v1"
M1_SLEEVE = "m1"
M1_PUBLISHED_PROFILE = "research-m1"
M1_COEFFICIENT_SET_ID = "m1-deploy-coefs-2026-10-28"
M1_LEAD_BUCKET = "day-ahead"
M1_HALT_EVENT = "M1_HALT"
M1_FILL_MODEL = "immediate_visible_quote"

# The deployment refit on all data 2026-07-19..2026-10-28
# (docs/research/m1_deploy_market_recal_coefs.json, entries
# "refit_on_all_2026-07-19_to_10-28 (for deployment)" -> market-L1a..L1d).
# (bucket, a, c). Frozen for the whole run; a refit is a new policy.
M1_COEFFICIENTS: tuple[tuple[str, float, float], ...] = (
    ("L1a", 0.2991, 1.2792),
    ("L1b", 0.2160, 1.2058),
    ("L1c", 0.2247, 1.2131),
    ("L1d", 0.2378, 1.2439),
)

# Stop-rule identifiers. Each is recorded at most once as an M1_HALT ledger
# event; none resumes automatically.
STOP_FIDELITY = "fidelity"
STOP_LOSS = "loss_stop"
STOP_FUTILITY = "futility"
STOP_PREMISE = "premise"
STOP_FINAL_READ = "final_read"
STOP_RULES = (STOP_FIDELITY, STOP_LOSS, STOP_FUTILITY, STOP_PREMISE, STOP_FINAL_READ)

# Evaluation outcomes recorded per sampled leg.
DECISION_FILTERED = "FILTERED"
DECISION_RISK_BLOCKED = "RISK_BLOCKED"
DECISION_NO_SIGNAL = "NO_SIGNAL"
DECISION_PENDING = "PENDING_ADMISSION"
DECISION_ENTERED = "ENTERED"
DECISION_SHADOW = "SHADOW"
DECISION_ALREADY_ENTERED = "ALREADY_ENTERED"
DECISION_ACCOUNT_BLOCKED = "ACCOUNT_BLOCKED"
DECISION_HALTED = "HALTED"
# Admission raised (a sqlite lock, a crash in the admission code): no order
# exists, and the leg stays eligible at its next sampled crossing.
DECISION_ADMISSION_ERROR = "ADMISSION_ERROR"
# The leg's first crossing was already recorded in shadow mode, so it is never
# entered later: the rule enters at the first crossing only.
DECISION_ALREADY_CROSSED = "ALREADY_CROSSED"

# Shadow arms (no paper fills).
ARM_FIVE_MINUTE = "m1_5min"
ARM_MODEL_VETO = "mv_hourly"
SHADOW_ARMS = (ARM_FIVE_MINUTE, ARM_MODEL_VETO)

# Pre-registered analysis constants (section 6 of the pre-registration).
ALPHA_PRIMARY = 0.025
ALPHA_AGREE = 0.0125
ALPHA_AGREE_MINUS_DISAGREE = 0.0125
FINAL_READ_BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261102

_EPS = 1e-9
_NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class M1TrialPolicy:
    """Immutable, fingerprinted controls for the M1 paper trial.

    Every execution-relevant constant is a field so the fingerprint changes
    with any of them. The risk values were copied from the research profile on
    2026-11-02 and are pinned here so a later research-profile change cannot
    silently move a frozen trial (a test compares them with the profile).
    """

    account_id: str
    policy_version: str
    sleeve: str
    reference_equity: float
    coefficient_set_id: str
    coefficients: tuple[tuple[str, float, float], ...]
    logit_clip_low: float
    logit_clip_high: float
    edge_threshold: float
    taker_fee_rate: float
    gate_fee: str
    pnl_fee: str
    lead_days: int
    sampling: str
    listing_hour_timezone: str
    listing_hour: int
    min_entry_bid: float
    max_entry_ask: float
    max_spread_cents: int
    min_ask_size: float
    min_bid_size: float
    max_model_market_gap: float
    max_source_spread_f: float
    max_forecast_age_hours: float
    stake_dollars: float
    max_contracts: int
    max_position_risk_pct: float
    max_event_risk_pct: float
    max_target_date_risk_pct: float
    daily_loss_pause_pct: float
    one_entry_per_leg: bool
    execution: str
    exits: str
    cumulative_loss_stop: float
    futility_min_entries: int
    futility_max_roi: float
    premise_window_dates: int
    premise_min_slope: float
    final_read_entries: int
    final_read_trading_days: int
    fidelity_weekly_failures: int
    fidelity_window_days: int
    fidelity_price_tolerance: float
    real_money_eligible: bool

    def coefficient(self, bucket: str) -> tuple[float, float]:
        for name, a, c in self.coefficients:
            if name == bucket:
                return a, c
        raise ValueError(f"no M1 coefficient for bucket {bucket!r}")

    def identity_payload(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "coefficient_set_id": self.coefficient_set_id,
            "coefficients": [[name, a, c] for name, a, c in self.coefficients],
            "cumulative_loss_stop": self.cumulative_loss_stop,
            "daily_loss_pause_pct": self.daily_loss_pause_pct,
            "edge_threshold": self.edge_threshold,
            "execution": self.execution,
            "exits": self.exits,
            "fidelity_price_tolerance": self.fidelity_price_tolerance,
            "fidelity_weekly_failures": self.fidelity_weekly_failures,
            "fidelity_window_days": self.fidelity_window_days,
            "final_read_entries": self.final_read_entries,
            "final_read_trading_days": self.final_read_trading_days,
            "futility_max_roi": self.futility_max_roi,
            "futility_min_entries": self.futility_min_entries,
            "gate_fee": self.gate_fee,
            "lead_days": self.lead_days,
            "listing_hour": self.listing_hour,
            "listing_hour_timezone": self.listing_hour_timezone,
            "logit_clip_high": self.logit_clip_high,
            "logit_clip_low": self.logit_clip_low,
            "max_contracts": self.max_contracts,
            "max_entry_ask": self.max_entry_ask,
            "max_event_risk_pct": self.max_event_risk_pct,
            "max_forecast_age_hours": self.max_forecast_age_hours,
            "max_model_market_gap": self.max_model_market_gap,
            "max_position_risk_pct": self.max_position_risk_pct,
            "max_source_spread_f": self.max_source_spread_f,
            "max_spread_cents": self.max_spread_cents,
            "max_target_date_risk_pct": self.max_target_date_risk_pct,
            "min_ask_size": self.min_ask_size,
            "min_bid_size": self.min_bid_size,
            "min_entry_bid": self.min_entry_bid,
            "one_entry_per_leg": self.one_entry_per_leg,
            "pnl_fee": self.pnl_fee,
            "policy_version": self.policy_version,
            "premise_min_slope": self.premise_min_slope,
            "premise_window_dates": self.premise_window_dates,
            "real_money_eligible": self.real_money_eligible,
            "reference_equity": self.reference_equity,
            "sampling": self.sampling,
            "sleeve": self.sleeve,
            "stake_dollars": self.stake_dollars,
            "taker_fee_rate": self.taker_fee_rate,
        }

    @property
    def policy_fingerprint(self) -> str:
        encoded = json.dumps(
            self.identity_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    @property
    def position_cap_dollars(self) -> float:
        return self.reference_equity * self.max_position_risk_pct

    @property
    def event_cap_dollars(self) -> float:
        return self.reference_equity * self.max_event_risk_pct

    @property
    def target_date_cap_dollars(self) -> float:
        return self.reference_equity * self.max_target_date_risk_pct

    @property
    def daily_loss_limit_dollars(self) -> float:
        return -self.reference_equity * self.daily_loss_pause_pct


M1_POLICY = M1TrialPolicy(
    account_id=M1_ACCOUNT_ID,
    policy_version=M1_POLICY_VERSION,
    sleeve=M1_SLEEVE,
    reference_equity=2_000.0,
    coefficient_set_id=M1_COEFFICIENT_SET_ID,
    coefficients=M1_COEFFICIENTS,
    logit_clip_low=0.005,
    logit_clip_high=0.995,
    edge_threshold=0.02,
    taker_fee_rate=0.07,
    gate_fee="one-contract-ceil-cent",
    pnl_fee="kalshi-per-order-ceil-cent",
    lead_days=1,
    sampling="first-research-scan-per-leg-per-utc-hour",
    listing_hour_timezone="America/New_York",
    listing_hour=10,
    min_entry_bid=0.01,
    max_entry_ask=0.99,
    max_spread_cents=10,
    min_ask_size=1.0,
    min_bid_size=1.0,
    max_model_market_gap=0.25,
    max_source_spread_f=7.3,
    max_forecast_age_hours=30.0,
    stake_dollars=10.0,
    max_contracts=25,
    max_position_risk_pct=0.015,
    max_event_risk_pct=0.03,
    max_target_date_risk_pct=0.05,
    daily_loss_pause_pct=0.15,
    one_entry_per_leg=True,
    execution="taker-displayed-ask-immediate",
    exits="hold-to-settlement",
    cumulative_loss_stop=-300.0,
    futility_min_entries=400,
    futility_max_roi=-0.05,
    premise_window_dates=30,
    premise_min_slope=1.05,
    final_read_entries=1000,
    final_read_trading_days=60,
    fidelity_weekly_failures=2,
    fidelity_window_days=7,
    fidelity_price_tolerance=0.01,
    real_money_eligible=False,
)


# --------------------------------------------------------------------------
# Math
# --------------------------------------------------------------------------


def logit_clipped(
    probability: float,
    *,
    low: float = M1_POLICY.logit_clip_low,
    high: float = M1_POLICY.logit_clip_high,
) -> float:
    p = min(max(float(probability), low), high)
    return math.log(p / (1.0 - p))


def bucket_for_local_hour(local_hour: float) -> str:
    """Day-ahead bucket from the station's civil local hour on D-1."""

    if local_hour < 11:
        return "L1a"
    if local_hour < 15:
        return "L1b"
    if local_hour < 19:
        return "L1c"
    return "L1d"


def recalibrated_p_yes(
    q_yes: float, bucket: str, *, policy: M1TrialPolicy = M1_POLICY
) -> float:
    a, c = policy.coefficient(bucket)
    return 1.0 / (
        1.0
        + math.exp(
            -(
                a
                + c
                * logit_clipped(
                    q_yes, low=policy.logit_clip_low, high=policy.logit_clip_high
                )
            )
        )
    )


def gate_fee(
    ask: float, *, series_ticker: str | None = None, policy: M1TrialPolicy = M1_POLICY
) -> float:
    """One-contract taker fee rounded up to the cent (the threshold's fee)."""

    return quadratic_fee_per_contract(
        ask, taker_rate=policy.taker_fee_rate, series_ticker=series_ticker
    )


def order_fee(
    ask: float,
    contracts: float,
    *,
    series_ticker: str | None = None,
    policy: M1TrialPolicy = M1_POLICY,
) -> float:
    """Kalshi's per-order taker fee: ceil(0.07 * C * P * (1 - P) * 100) / 100."""

    return quadratic_fee_total(
        ask, contracts, taker_rate=policy.taker_fee_rate, series_ticker=series_ticker
    )


def spread_cents(ask: float, bid: float) -> int:
    """Side spread in integer cents (float-safe, as validated)."""

    return int(round((float(ask) - float(bid)) * 100.0))


def size_contracts(
    ask: float,
    ask_size: float,
    *,
    series_ticker: str | None = None,
    policy: M1TrialPolicy = M1_POLICY,
) -> int:
    """$10 notional including the order fee, clamped to depth and 25 contracts."""

    try:
        ask = float(ask)
        ask_size = float(ask_size)
    except (TypeError, ValueError):
        return 0
    if not (math.isfinite(ask) and math.isfinite(ask_size)) or not 0.0 < ask < 1.0:
        return 0
    stake = policy.stake_dollars
    contracts = math.floor(
        stake / (ask + policy.taker_fee_rate * ask * (1.0 - ask)) + _EPS
    )
    while contracts > 0 and (
        contracts * ask
        + order_fee(ask, contracts, series_ticker=series_ticker, policy=policy)
        > stake + _EPS
    ):
        contracts -= 1
    depth = math.floor(ask_size + _EPS) if ask_size > 0 else 0
    return max(0, min(contracts, depth, policy.max_contracts))


def entry_stake(
    ask: float,
    contracts: int,
    *,
    series_ticker: str | None = None,
    policy: M1TrialPolicy = M1_POLICY,
) -> float:
    if contracts <= 0:
        return 0.0
    return contracts * ask + order_fee(
        ask, contracts, series_ticker=series_ticker, policy=policy
    )


# --------------------------------------------------------------------------
# Clocks
# --------------------------------------------------------------------------


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("M1 timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_utc(value) if value.tzinfo is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def utc_hour_key(created_at: datetime) -> str:
    return _aware_utc(created_at).strftime("%Y-%m-%dT%H")


def civil_local_time(created_at: datetime, civil_tz: str) -> datetime:
    return _aware_utc(created_at).astimezone(ZoneInfo(civil_tz))


def civil_local_hour(created_at: datetime, civil_tz: str) -> float:
    """Hour plus minutes/60 on the station's civil clock (as the research did)."""

    local = civil_local_time(created_at, civil_tz)
    return local.hour + local.minute / 60.0


def civil_lead_days(target_date: date, created_at: datetime, civil_tz: str) -> int:
    return (target_date - civil_local_time(created_at, civil_tz).date()).days


def is_listing_hour(
    target_date: date, created_at: datetime, *, policy: M1TrialPolicy = M1_POLICY
) -> bool:
    """D-1 10:00-10:59 America/New_York, when Kalshi lists the next ladder."""

    local = _aware_utc(created_at).astimezone(ZoneInfo(policy.listing_hour_timezone))
    return (
        local.date() == target_date - timedelta(days=1)
        and local.hour == policy.listing_hour
    )


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class M1LegInputs:
    """Everything the rule reads for one leg at one scan, as recorded."""

    created_at: datetime
    target_date: date
    market_ticker: str
    side: str
    civil_tz: str
    market_probability: float | None
    model_probability: float | None
    ask: float | None
    bid: float | None
    ask_size: float | None
    bid_size: float | None
    market_status: str | None
    market_close_time: str | None
    forecast_age_hours: float | None
    source_spread_f: float | None
    entry_gate_reason: str | None = None

    @property
    def series_ticker(self) -> str:
        return str(self.market_ticker).split("-", 1)[0].upper()

    @property
    def event_ticker(self) -> str:
        parts = str(self.market_ticker).split("-")
        return "-".join(parts[:2]).upper() if len(parts) >= 2 else str(self.market_ticker)


@dataclass(frozen=True)
class M1LegEvaluation:
    inputs: M1LegInputs
    utc_hour: str
    local_hour: float
    lead_days: int
    listing_hour: bool
    bucket: str
    q_yes: float | None
    p_yes: float | None
    p_trade: float | None
    gate_fee: float | None
    cost: float | None
    edge: float | None
    spread_cents: int | None
    model_market_gap: float | None
    agree: bool | None
    signal: bool
    filter_reason: str | None
    risk_reason: str | None
    contracts: int
    stake: float
    order_fee: float

    @property
    def passes(self) -> bool:
        """The leg-level rule: filters, per-leg risk gates, signal and size."""

        return (
            self.filter_reason is None
            and self.risk_reason is None
            and self.signal
            and self.contracts > 0
        )

    @property
    def decision(self) -> str:
        if self.filter_reason is not None:
            return DECISION_FILTERED
        if self.risk_reason is not None:
            return DECISION_RISK_BLOCKED
        if not self.signal:
            return DECISION_NO_SIGNAL
        if self.contracts <= 0:
            return DECISION_FILTERED
        return DECISION_PENDING

    @property
    def reason(self) -> str | None:
        if self.filter_reason is not None:
            return self.filter_reason
        if self.risk_reason is not None:
            return self.risk_reason
        if not self.signal:
            edge = "unavailable" if self.edge is None else f"{self.edge:.4f}"
            return f"edge {edge} below {M1_POLICY.edge_threshold:.2f}"
        if self.contracts <= 0:
            return "sizing produced zero contracts"
        return None

    @property
    def leg(self) -> tuple[str, str, str]:
        return (
            self.inputs.target_date.isoformat(),
            self.inputs.market_ticker,
            self.inputs.side,
        )


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _market_is_open(inputs: M1LegInputs) -> tuple[bool, str | None]:
    status = str(inputs.market_status or "").strip().lower()
    if status != "active":
        return False, f"market status is {status or 'unknown'}, not active"
    close_at = parse_timestamp(inputs.market_close_time)
    if close_at is not None and _aware_utc(inputs.created_at) >= close_at:
        return False, "market is past its close time"
    return True, None


def evaluate_leg(
    inputs: M1LegInputs, *, policy: M1TrialPolicy = M1_POLICY
) -> M1LegEvaluation:
    """Apply the frozen M1 rule to one leg at one scan.

    Filters and per-leg risk gates are reported in a fixed order so a recorded
    reason is reproducible. Account-level limits (capacity, the exposure caps,
    the daily-loss pause, halts, one entry per leg) are applied at admission.
    """

    side = str(inputs.side or "").upper()
    created_at = _aware_utc(inputs.created_at)
    local_hour = civil_local_hour(created_at, inputs.civil_tz)
    lead = civil_lead_days(inputs.target_date, created_at, inputs.civil_tz)
    listing = is_listing_hour(inputs.target_date, created_at, policy=policy)
    bucket = bucket_for_local_hour(local_hour)
    market_probability = _finite(inputs.market_probability)
    model_probability = _finite(inputs.model_probability)
    ask = _finite(inputs.ask)
    bid = _finite(inputs.bid)
    ask_size = _finite(inputs.ask_size)
    bid_size = _finite(inputs.bid_size)

    q_yes = p_yes = p_trade = fee = cost = edge = None
    if market_probability is not None and side in {"YES", "NO"}:
        q_yes = market_probability if side == "YES" else 1.0 - market_probability
        p_yes = recalibrated_p_yes(q_yes, bucket, policy=policy)
        p_trade = p_yes if side == "YES" else 1.0 - p_yes
    if ask is not None and 0.0 < ask < 1.0:
        fee = gate_fee(ask, series_ticker=inputs.series_ticker, policy=policy)
        cost = ask + fee
        if p_trade is not None:
            edge = p_trade - cost
    spread = spread_cents(ask, bid) if ask is not None and bid is not None else None
    gap = (
        abs(model_probability - market_probability)
        if model_probability is not None and market_probability is not None
        else None
    )
    agree = (
        model_probability >= cost
        if model_probability is not None and cost is not None
        else None
    )
    signal = edge is not None and edge >= policy.edge_threshold

    filter_reason: str | None = None
    open_market, closed_reason = _market_is_open(inputs)
    if side not in {"YES", "NO"}:
        filter_reason = "side must be YES or NO"
    elif lead != policy.lead_days:
        filter_reason = f"lead {lead} is not day-ahead (lead {policy.lead_days})"
    elif listing:
        filter_reason = "listing hour (D-1 10:00-10:59 America/New_York)"
    elif not open_market:
        filter_reason = closed_reason
    elif market_probability is None or not 0.0 <= market_probability <= 1.0:
        filter_reason = "no same-scan de-vigged market probability"
    elif bid is None or bid < policy.min_entry_bid - _EPS:
        filter_reason = f"entry bid below {policy.min_entry_bid:.2f}"
    elif ask is None or not 0.0 < ask <= policy.max_entry_ask + _EPS:
        filter_reason = f"entry ask outside (0, {policy.max_entry_ask:.2f}]"
    elif spread is None or spread > policy.max_spread_cents:
        filter_reason = f"spread {spread}c above {policy.max_spread_cents}c"
    elif cost is None or cost >= 1.0:
        filter_reason = "all-in cost meets or exceeds the $1 payout"
    elif ask_size is None or ask_size < policy.min_ask_size - _EPS:
        filter_reason = f"displayed ask size below {policy.min_ask_size:g}"

    risk_reason: str | None = None
    if filter_reason is None:
        age = _finite(inputs.forecast_age_hours)
        source_spread = _finite(inputs.source_spread_f)
        if bid_size is None or bid_size < policy.min_bid_size - _EPS:
            risk_reason = f"bid size below {policy.min_bid_size:g}"
        elif gap is None:
            risk_reason = "model/market gap unavailable"
        elif gap > policy.max_model_market_gap + _EPS:
            risk_reason = (
                f"model/market gap {gap:.3f} exceeds {policy.max_model_market_gap:.2f}"
            )
        elif source_spread is not None and source_spread > policy.max_source_spread_f + _EPS:
            risk_reason = (
                f"forecast source spread {source_spread:.1f}F exceeds "
                f"{policy.max_source_spread_f:.1f}F"
            )
        elif age is None:
            risk_reason = "forecast age unavailable"
        elif age > policy.max_forecast_age_hours + _EPS:
            risk_reason = (
                f"forecast {age:.1f}h old exceeds {policy.max_forecast_age_hours:.0f}h"
            )
        elif inputs.entry_gate_reason:
            risk_reason = str(inputs.entry_gate_reason)

    contracts = 0
    stake = 0.0
    fee_total = 0.0
    if ask is not None and ask_size is not None and 0.0 < ask < 1.0:
        contracts = size_contracts(
            ask, ask_size, series_ticker=inputs.series_ticker, policy=policy
        )
        if contracts > 0:
            fee_total = order_fee(
                ask, contracts, series_ticker=inputs.series_ticker, policy=policy
            )
            stake = contracts * ask + fee_total

    return M1LegEvaluation(
        inputs=inputs,
        utc_hour=utc_hour_key(created_at),
        local_hour=local_hour,
        lead_days=lead,
        listing_hour=listing,
        bucket=bucket,
        q_yes=q_yes,
        p_yes=p_yes,
        p_trade=p_trade,
        gate_fee=fee,
        cost=cost,
        edge=edge,
        spread_cents=spread,
        model_market_gap=gap,
        agree=agree,
        signal=signal,
        filter_reason=filter_reason,
        risk_reason=risk_reason,
        contracts=contracts,
        stake=stake,
        order_fee=fee_total,
    )


# --------------------------------------------------------------------------
# Daily replay
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedEvaluation:
    """One sampled evaluation as the durable M1 record holds it."""

    evaluation_id: int
    inputs: M1LegInputs
    placement_enabled: bool
    recorded_decision: str
    recorded_reason: str | None = None
    recorded_ask: float | None = None


@dataclass(frozen=True)
class ExpectedEntry:
    evaluation_id: int
    leg: tuple[str, str, str]
    utc_hour: str
    ask: float
    contracts: int


@dataclass(frozen=True)
class ActualEntry:
    order_id: int
    leg: tuple[str, str, str]
    created_at: datetime
    entry_price: float
    entry_bid: float | None
    civil_tz: str | None


# Recorded account-level outcomes the replay takes as given: the leg rule is
# recomputed from raw inputs, but cash, caps, pauses, halts and admission
# failures are account state the evaluation row cannot reproduce on its own.
# A PENDING row that reaches the replay never got an order (a crash between
# committing it and admitting it), so it is account-level too: no entry was
# made and the leg stays eligible at its next sampled crossing.
_ACCOUNT_LEVEL_DECISIONS = frozenset(
    {
        DECISION_ACCOUNT_BLOCKED,
        DECISION_HALTED,
        DECISION_ADMISSION_ERROR,
        DECISION_PENDING,
    }
)


def replay_expected_entries(
    evaluations: Sequence[RecordedEvaluation],
    *,
    already_crossed: Iterable[tuple[str, str, str]] = (),
    policy: M1TrialPolicy = M1_POLICY,
) -> tuple[list[ExpectedEntry], list[dict[str, object]]]:
    """Replay recorded sampled evaluations through the same rule.

    A leg is crossed at its first passing sampled evaluation. With placement
    enabled that is its entry (unless an account-level limit or an admission
    failure stopped it, in which case the next sampled crossing may enter);
    with placement disabled the crossing is recorded in shadow mode and the
    leg is never entered later. Returns the entries the rule implies and every
    row whose recorded decision the recomputation contradicts.
    """

    crossed = set(already_crossed)
    expected: list[ExpectedEntry] = []
    contradictions: list[dict[str, object]] = []
    ordered = sorted(
        evaluations,
        key=lambda row: (_aware_utc(row.inputs.created_at), row.evaluation_id),
    )
    for row in ordered:
        evaluation = evaluate_leg(row.inputs, policy=policy)
        leg = evaluation.leg
        recorded_entered = row.recorded_decision == DECISION_ENTERED
        if leg in crossed:
            if recorded_entered:
                contradictions.append(
                    {
                        "evaluation_id": row.evaluation_id,
                        "leg": list(leg),
                        "issue": "recorded entry on an already-crossed leg",
                    }
                )
            continue
        if not evaluation.passes:
            if recorded_entered:
                contradictions.append(
                    {
                        "evaluation_id": row.evaluation_id,
                        "leg": list(leg),
                        "issue": "recorded entry that the rule rejects",
                        "replayed_reason": evaluation.reason,
                    }
                )
            continue
        if not row.placement_enabled:
            crossed.add(leg)
            continue
        if row.recorded_decision in _ACCOUNT_LEVEL_DECISIONS:
            continue
        expected.append(
            ExpectedEntry(
                evaluation_id=row.evaluation_id,
                leg=leg,
                utc_hour=evaluation.utc_hour,
                ask=float(row.inputs.ask),
                contracts=evaluation.contracts,
            )
        )
        crossed.add(leg)
    return expected, contradictions


def order_breaches(
    entry: ActualEntry, *, policy: M1TrialPolicy = M1_POLICY
) -> list[str]:
    """Immediate fidelity breaches visible on one paper entry by itself."""

    breaches: list[str] = []
    target = date.fromisoformat(entry.leg[0])
    if is_listing_hour(target, entry.created_at, policy=policy):
        breaches.append("entry in the ET listing hour")
    if entry.entry_bid is not None:
        cents = spread_cents(entry.entry_price, entry.entry_bid)
        if cents > policy.max_spread_cents:
            breaches.append(f"entry spread {cents}c above {policy.max_spread_cents}c")
    if entry.civil_tz:
        lead = civil_lead_days(target, entry.created_at, entry.civil_tz)
        if lead != policy.lead_days:
            breaches.append(f"entry lead {lead} is not {policy.lead_days}")
    else:
        breaches.append("entry station clock unknown")
    return breaches


def match_entries(
    expected: Sequence[ExpectedEntry],
    actual: Sequence[ActualEntry],
    *,
    policy: M1TrialPolicy = M1_POLICY,
) -> dict[str, object]:
    """Compare paper entries with the replay (same leg, UTC hour, price +/- 1c)."""

    expected_by_leg = {entry.leg: entry for entry in expected}
    matched: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    breaches: list[dict[str, object]] = []
    seen_legs: dict[tuple[str, str, str], int] = {}
    for entry in sorted(actual, key=lambda row: (row.created_at, row.order_id)):
        for breach in order_breaches(entry, policy=policy):
            breaches.append(
                {"order_id": entry.order_id, "leg": list(entry.leg), "breach": breach}
            )
        if entry.leg in seen_legs:
            breaches.append(
                {
                    "order_id": entry.order_id,
                    "leg": list(entry.leg),
                    "breach": f"re-entry of a leg first entered by order {seen_legs[entry.leg]}",
                }
            )
            failed.append(
                {"order_id": entry.order_id, "leg": list(entry.leg), "issue": "re-entry"}
            )
            continue
        seen_legs[entry.leg] = entry.order_id
        replayed = expected_by_leg.get(entry.leg)
        hour = utc_hour_key(entry.created_at)
        if replayed is None:
            failed.append(
                {
                    "order_id": entry.order_id,
                    "leg": list(entry.leg),
                    "issue": "no replayed entry for this leg",
                }
            )
            continue
        if replayed.utc_hour != hour:
            failed.append(
                {
                    "order_id": entry.order_id,
                    "leg": list(entry.leg),
                    "issue": f"UTC hour {hour} differs from replayed {replayed.utc_hour}",
                }
            )
            continue
        if abs(float(entry.entry_price) - replayed.ask) > policy.fidelity_price_tolerance + _EPS:
            failed.append(
                {
                    "order_id": entry.order_id,
                    "leg": list(entry.leg),
                    "issue": (
                        f"price {entry.entry_price:.2f} differs from replayed "
                        f"{replayed.ask:.2f} by more than 1c"
                    ),
                }
            )
            continue
        matched.append(
            {
                "order_id": entry.order_id,
                "evaluation_id": replayed.evaluation_id,
                "leg": list(entry.leg),
            }
        )
    actual_legs = {entry.leg for entry in actual}
    missed = [
        {
            "evaluation_id": entry.evaluation_id,
            "leg": list(entry.leg),
            "utc_hour": entry.utc_hour,
            "ask": entry.ask,
        }
        for entry in expected
        if entry.leg not in actual_legs
    ]
    return {
        "matched": matched,
        "failed": failed,
        "missed": missed,
        "breaches": breaches,
    }


# --------------------------------------------------------------------------
# Statistics for the stop rules and the pre-registered reads
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SettledEntry:
    target_date: str
    pnl: float
    stake: float
    agree: bool | None = None


def roi(entries: Sequence[SettledEntry]) -> float | None:
    stake = math.fsum(entry.stake for entry in entries)
    if stake <= 0:
        return None
    return math.fsum(entry.pnl for entry in entries) / stake


def _date_totals(entries: Sequence[SettledEntry]) -> dict[str, tuple[float, float]]:
    totals: dict[str, list[float]] = {}
    for entry in entries:
        bucket = totals.setdefault(entry.target_date, [0.0, 0.0])
        bucket[0] += entry.pnl
        bucket[1] += entry.stake
    return {key: (value[0], value[1]) for key, value in totals.items()}


def date_clustered_bootstrap(
    entries: Sequence[SettledEntry],
    *,
    resamples: int = FINAL_READ_BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float | int | None]:
    """ROI with a bootstrap that resamples whole target dates."""

    totals = _date_totals(entries)
    dates = sorted(totals)
    point = roi(entries)
    if not dates or point is None:
        return {"roi": point, "lower": None, "upper": None, "p_le_zero": None, "dates": 0}
    rng = random.Random(seed)
    pnl = [totals[day][0] for day in dates]
    stake = [totals[day][1] for day in dates]
    n = len(dates)
    stats: list[float] = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in range(n)]
        s = math.fsum(stake[i] for i in picks)
        if s <= 0:
            continue
        stats.append(math.fsum(pnl[i] for i in picks) / s)
    return _summarize_bootstrap(point, stats, n)


def date_clustered_difference_bootstrap(
    first: Sequence[SettledEntry],
    second: Sequence[SettledEntry],
    *,
    resamples: int = FINAL_READ_BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float | int | None]:
    """ROI(first) - ROI(second), resampling the union of target dates jointly."""

    a_totals = _date_totals(first)
    b_totals = _date_totals(second)
    dates = sorted(set(a_totals) | set(b_totals))
    a_point, b_point = roi(first), roi(second)
    if not dates or a_point is None or b_point is None:
        return {"roi": None, "lower": None, "upper": None, "p_le_zero": None, "dates": len(dates)}
    rng = random.Random(seed)
    n = len(dates)
    a_pnl = [a_totals.get(day, (0.0, 0.0))[0] for day in dates]
    a_stake = [a_totals.get(day, (0.0, 0.0))[1] for day in dates]
    b_pnl = [b_totals.get(day, (0.0, 0.0))[0] for day in dates]
    b_stake = [b_totals.get(day, (0.0, 0.0))[1] for day in dates]
    stats: list[float] = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in range(n)]
        sa = math.fsum(a_stake[i] for i in picks)
        sb = math.fsum(b_stake[i] for i in picks)
        if sa <= 0 or sb <= 0:
            continue
        stats.append(
            math.fsum(a_pnl[i] for i in picks) / sa
            - math.fsum(b_pnl[i] for i in picks) / sb
        )
    return _summarize_bootstrap(a_point - b_point, stats, n)


def _summarize_bootstrap(
    point: float, stats: list[float], dates: int
) -> dict[str, float | int | None]:
    if not stats:
        return {"roi": point, "lower": None, "upper": None, "p_le_zero": None, "dates": dates}
    ordered = sorted(stats)

    def percentile(q: float) -> float:
        position = q * (len(ordered) - 1)
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {
        "roi": point,
        "lower": percentile(0.025),
        "upper": percentile(0.975),
        "p_le_zero": sum(1 for value in stats if value <= 0.0) / len(stats),
        "dates": dates,
    }


def weighted_logistic_fit(
    xs: Sequence[float],
    ys: Sequence[float],
    weights: Sequence[float],
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-10,
) -> tuple[float, float] | None:
    """Unpenalized weighted logistic regression y ~ a + c * x (Newton-Raphson)."""

    if not (len(xs) == len(ys) == len(weights)) or len(xs) < 2:
        return None
    a = c = 0.0

    def loglik(a_value: float, c_value: float) -> float:
        total = 0.0
        for x, y, w in zip(xs, ys, weights):
            z = a_value + c_value * x
            # log(1 + exp(z)) without overflow.
            softplus = z + math.log1p(math.exp(-z)) if z > 0 else math.log1p(math.exp(z))
            total += w * (y * z - softplus)
        return total

    current = loglik(a, c)
    for _ in range(max_iterations):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y, w in zip(xs, ys, weights):
            z = a + c * x
            p = 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))
            residual = w * (y - p)
            g0 += residual
            g1 += residual * x
            v = w * p * (1.0 - p)
            h00 += v
            h01 += v * x
            h11 += v * x * x
        determinant = h00 * h11 - h01 * h01
        if not math.isfinite(determinant) or abs(determinant) < 1e-300:
            return None
        step_a = (h11 * g0 - h01 * g1) / determinant
        step_c = (h00 * g1 - h01 * g0) / determinant
        scale = 1.0
        while scale > 1e-8:
            trial_a = a + scale * step_a
            trial_c = c + scale * step_c
            trial = loglik(trial_a, trial_c)
            if trial >= current - 1e-12:
                break
            scale /= 2.0
        else:
            return None
        a, c = trial_a, trial_c
        improvement = trial - current
        current = trial
        if max(abs(scale * step_a), abs(scale * step_c)) < tolerance or abs(improvement) < tolerance:
            return a, c
    return a, c


@dataclass(frozen=True)
class PremiseRow:
    target_date: str
    market_ticker: str
    scan_key: str
    side: str
    q_yes: float
    resolves_yes: bool
    tradeable: bool


def premise_slope(rows: Iterable[PremiseRow]) -> dict[str, object]:
    """Pooled day-ahead recalibration slope, fitted the way the coefficients were.

    Tradeable sampled rows only; one YES-view row per (target date, market,
    scan), preferring the YES leg; each market-day carries total weight one.
    """

    chosen: dict[tuple[str, str, str], PremiseRow] = {}
    for row in rows:
        if not row.tradeable:
            continue
        key = (row.target_date, row.market_ticker, row.scan_key)
        current = chosen.get(key)
        if current is None or (current.side != "YES" and row.side == "YES"):
            chosen[key] = row
    counts: dict[tuple[str, str], int] = {}
    for row in chosen.values():
        counts[(row.target_date, row.market_ticker)] = (
            counts.get((row.target_date, row.market_ticker), 0) + 1
        )
    xs: list[float] = []
    ys: list[float] = []
    ws: list[float] = []
    for row in chosen.values():
        xs.append(logit_clipped(row.q_yes))
        ys.append(1.0 if row.resolves_yes else 0.0)
        ws.append(1.0 / counts[(row.target_date, row.market_ticker)])
    fit = weighted_logistic_fit(xs, ys, ws) if xs else None
    return {
        "rows": len(xs),
        "market_days": len(counts),
        "target_dates": len({row.target_date for row in chosen.values()}),
        "intercept": None if fit is None else fit[0],
        "slope": None if fit is None else fit[1],
    }


def resolves_yes(
    strike_type: object,
    floor_strike: object,
    cap_strike: object,
    settlement_high_f: float,
) -> bool | None:
    """Kalshi bracket outcome through the same rule paper settlement books.

    ``None`` when the bracket has no usable strikes, so a malformed row never
    becomes evidence.
    """

    kind = str(strike_type or "").strip().lower()
    floor = _finite(floor_strike)
    cap = _finite(cap_strike)
    if (kind == "greater" and floor is None) or (kind == "less" and cap is None):
        return None
    if kind not in {"greater", "less"} and (floor is None or cap is None):
        return None
    return bin_resolves_yes(
        kind, floor, cap, integer_settlement_high_f(settlement_high_f)
    )


def policy_summary(policy: M1TrialPolicy = M1_POLICY) -> Mapping[str, object]:
    """Dollar values of every cap, for publication and the pre-registration."""

    return {
        "stake_dollars": policy.stake_dollars,
        "max_contracts_per_market": policy.max_contracts,
        "position_cap_dollars": round(policy.position_cap_dollars, 2),
        "event_cap_dollars": round(policy.event_cap_dollars, 2),
        "target_date_cap_dollars": round(policy.target_date_cap_dollars, 2),
        "daily_loss_pause_dollars": round(-policy.daily_loss_limit_dollars, 2),
        "cumulative_loss_stop_dollars": policy.cumulative_loss_stop,
        "max_spread_cents": policy.max_spread_cents,
        "min_entry_bid": policy.min_entry_bid,
        "max_entry_ask": policy.max_entry_ask,
        "min_ask_size": policy.min_ask_size,
        "min_bid_size": policy.min_bid_size,
        "max_model_market_gap": policy.max_model_market_gap,
        "max_source_spread_f": policy.max_source_spread_f,
        "max_forecast_age_hours": policy.max_forecast_age_hours,
    }
