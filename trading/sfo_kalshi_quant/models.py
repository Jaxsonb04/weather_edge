from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .settlement_truth import bin_resolves_yes


def _as_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    return float(value)


@dataclass(frozen=True)
class ForecastSnapshot:
    target_date: date
    predicted_high_f: float
    station_id: str = "KSFO"
    fetched_at: str | None = None
    lead_hours: float | None = None
    method: str = "unknown"
    google_high_f: float | None = None
    nws_high_f: float | None = None
    open_meteo_high_f: float | None = None
    history_high_f: float | None = None
    google_weight: float | None = None
    nws_weight: float | None = None
    open_meteo_weight: float | None = None
    history_weight: float | None = None
    station_adjustment_f: float | None = None
    fresh_station_count: int | None = None
    source_count: int = 0
    max_calls_per_day: int | None = None
    calls_used_today: int | None = None
    # EMOS-only snapshots have no blend source values; they carry the
    # cross-model NWP disagreement here so the uncertain-day source-spread gate
    # keeps meaning the same thing ("how much do independent forecasts differ").
    source_spread_override_f: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def source_values(self) -> list[float]:
        return [
            value
            for value in [
                self.google_high_f,
                self.nws_high_f,
                self.open_meteo_high_f,
                self.history_high_f,
            ]
            if value is not None
        ]

    @property
    def source_spread_f(self) -> float:
        if self.source_spread_override_f is not None:
            return float(self.source_spread_override_f)
        values = self.source_values
        if len(values) < 2:
            return 0.0
        return max(values) - min(values)

    def age_hours(self, now: datetime | None = None) -> float | None:
        if not self.fetched_at:
            return None
        try:
            fetched = datetime.fromisoformat(str(self.fetched_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return (now.astimezone(UTC) - fetched.astimezone(UTC)).total_seconds() / 3600.0


@dataclass(frozen=True)
class GoogleChallengerSnapshot:
    """Task 7: one immutable paired baseline/Google-challenger evidence row.

    Persisted ONLY as derived evidence -- station/target/issue identity, the
    fixed policy version, baseline and challenger (mu, sigma), bracket-key ->
    probability dicts, and the fixed action. Never a raw Google high, gap,
    response body, conditions text, URL, key, or token (spec section
    7.2/7.3); ``PaperStore.record_google_challenger_snapshot`` enforces that
    boundary on write, and the ``google_challenger_snapshots`` table has no
    column for any raw field in the first place.
    """

    station_id: str
    target_date: date
    issued_at: str
    policy_version: str
    baseline_mu: float
    baseline_sigma: float
    challenger_mu: float | None
    challenger_sigma: float
    baseline_probabilities: dict[str, float]
    challenger_probabilities: dict[str, float] | None
    action: str


@dataclass(frozen=True)
class ForecastOutcome:
    local_date: date
    predicted_high_f: float
    actual_high_f: float
    model_name: str = "lstm"
    station_id: str = "KSFO"

    @property
    def residual_f(self) -> float:
        return self.actual_high_f - self.predicted_high_f


@dataclass(frozen=True)
class IntradaySnapshot:
    target_date: date
    observed_high_f: float | None
    latest_temp_f: float | None
    latest_observed_at: str | None
    remaining_forecast_high_f: float | None
    forecast_fetched_at: str | None
    observation_count: int = 0
    observed_high_source: str | None = None
    is_complete: bool = False

    @property
    def has_observed_high(self) -> bool:
        return self.observed_high_f is not None


@dataclass(frozen=True)
class EnsembleSnapshot:
    target_date: date
    raw_member_highs_f: tuple[float, ...]
    station_member_highs_f: tuple[float, ...]
    raw_mean_high_f: float
    station_mean_high_f: float
    raw_std_high_f: float
    station_std_high_f: float
    station_bias_f: float
    grid_latitude: float | None
    grid_longitude: float | None
    grid_elevation_m: float | None
    cell_selection: str
    fetched_at: str | None = None
    source: str = "open_meteo_gfs_ensemble"
    warning: str | None = None

    @property
    def member_count(self) -> int:
        return len(self.station_member_highs_f)


@dataclass(frozen=True)
class MarketBin:
    ticker: str
    event_ticker: str
    title: str
    yes_sub_title: str
    strike_type: str
    floor_strike: float | None
    cap_strike: float | None
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_bid_size: float
    yes_ask_size: float
    status: str
    result: str = ""
    expiration_value: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_kalshi(cls, payload: dict[str, Any]) -> "MarketBin":
        expiration_value = payload.get("expiration_value")
        return cls(
            ticker=payload["ticker"],
            event_ticker=payload["event_ticker"],
            title=payload.get("title", ""),
            yes_sub_title=payload.get("yes_sub_title") or payload.get("subtitle", ""),
            strike_type=payload.get("strike_type", ""),
            floor_strike=_as_float(payload.get("floor_strike"), None),
            cap_strike=_as_float(payload.get("cap_strike"), None),
            yes_bid=_as_float(payload.get("yes_bid_dollars")),
            yes_ask=_as_float(payload.get("yes_ask_dollars"), 1.0),
            no_bid=_as_float(payload.get("no_bid_dollars")),
            no_ask=_as_float(payload.get("no_ask_dollars"), 1.0),
            yes_bid_size=_as_float(payload.get("yes_bid_size_fp")),
            yes_ask_size=_as_float(payload.get("yes_ask_size_fp")),
            status=payload.get("status", "unknown"),
            result=payload.get("result", ""),
            expiration_value=_as_float(expiration_value, None) if expiration_value not in (None, "") else None,
            raw=payload,
        )

    @property
    def spread(self) -> float:
        return max(0.0, self.yes_ask - self.yes_bid)

    @property
    def no_spread(self) -> float:
        return max(0.0, self.no_ask - self.no_bid)

    @property
    def no_bid_size(self) -> float:
        if "no_bid_size_fp" in self.raw:
            return _as_float(self.raw.get("no_bid_size_fp"))
        return self.yes_ask_size

    @property
    def no_ask_size(self) -> float:
        if "no_ask_size_fp" in self.raw:
            return _as_float(self.raw.get("no_ask_size_fp"))
        return self.yes_bid_size

    def side_bid(self, side: str) -> float:
        return self.yes_bid if side.upper() == "YES" else self.no_bid

    def side_ask(self, side: str) -> float:
        return self.yes_ask if side.upper() == "YES" else self.no_ask

    def side_bid_size(self, side: str) -> float:
        return self.yes_bid_size if side.upper() == "YES" else self.no_bid_size

    def side_ask_size(self, side: str) -> float:
        return self.yes_ask_size if side.upper() == "YES" else self.no_ask_size

    def side_spread(self, side: str) -> float:
        return self.spread if side.upper() == "YES" else self.no_spread

    @property
    def mid(self) -> float:
        if self.yes_bid <= 0 and self.yes_ask >= 1:
            return 0.5
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def sort_key(self) -> tuple[float, float]:
        lo, hi = self.continuous_interval()
        return (lo if math.isfinite(lo) else -999.0, hi if math.isfinite(hi) else 999.0)

    def continuous_interval(self) -> tuple[float, float]:
        """Approximate the CLISFO integer settlement bin as continuous degrees.

        Kalshi resolves off the integer NWS Daily Climate Report value. A
        continuous forecast distribution is mapped into integer bins using
        nearest-degree half-open thresholds.
        """

        if self.strike_type == "less":
            if self.cap_strike is None:
                return (-math.inf, math.inf)
            return (-math.inf, self.cap_strike - 0.5)
        if self.strike_type == "greater":
            if self.floor_strike is None:
                return (-math.inf, math.inf)
            return (self.floor_strike + 0.5, math.inf)
        if self.floor_strike is None or self.cap_strike is None:
            return (-math.inf, math.inf)
        return (self.floor_strike - 0.5, self.cap_strike + 0.5)

    def resolves_yes(self, settlement_high_f: float) -> bool:
        return bin_resolves_yes(
            self.strike_type,
            self.floor_strike,
            self.cap_strike,
            settlement_high_f,
        )


@dataclass(frozen=True)
class EventSnapshot:
    event_ticker: str
    title: str
    target_date: date | None
    markets: list[MarketBin]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_kalshi(cls, payload: dict[str, Any]) -> "EventSnapshot":
        target_date = target_date_from_event_ticker(payload.get("event_ticker", ""))
        markets = [MarketBin.from_kalshi(row) for row in payload.get("markets", [])]
        markets.sort(key=lambda market: market.sort_key)
        return cls(
            event_ticker=payload.get("event_ticker", ""),
            title=payload.get("title", ""),
            target_date=target_date,
            markets=markets,
            raw=payload,
        )

    @property
    def active_markets(self) -> list[MarketBin]:
        return [market for market in self.markets if market.status == "active"]


@dataclass(frozen=True)
class BucketProbability:
    ticker: str
    label: str
    probability: float
    lower_confidence: float
    empirical_probability: float
    normal_probability: float
    effective_n: float
    residual_probability: float | None = None
    ensemble_probability: float | None = None
    model_probability: float | None = None
    market_probability: float | None = None
    observed_high_f: float | None = None
    intraday_probability: float | None = None
    remaining_heat_risk: float | None = None
    # Whether observed_high_f is the complete official daily value (True) or a
    # raw nonfinal station maximum (False). None when no observation was used.
    # Nonfinal observations must never justify exact settlement certainty
    # (audit MD-01), and near-certain candidates built on them are gated.
    observed_high_is_final: bool | None = None


@dataclass(frozen=True)
class TradeDecision:
    ticker: str
    label: str
    action: str
    approved: bool
    probability: float
    probability_lcb: float
    yes_bid: float
    yes_ask: float
    spread: float
    fee_per_contract: float
    cost_per_contract: float
    edge: float
    edge_lcb: float
    kelly_fraction: float
    recommended_contracts: float
    expected_profit: float
    reasons: list[str]
    yes_ask_size: float = 0.0
    side: str = "YES"
    entry_bid: float | None = None
    entry_ask: float | None = None
    entry_bid_size: float | None = None
    entry_ask_size: float | None = None
    strike_type: str | None = None
    floor_strike: float | None = None
    cap_strike: float | None = None
    residual_probability: float | None = None
    ensemble_probability: float | None = None
    model_probability: float | None = None
    market_probability: float | None = None
    intraday_probability: float | None = None
    remaining_heat_risk: float | None = None
    trade_quality_score: float = 0.0
    limit_price: float | None = None
    limit_fee_per_contract: float | None = None
    limit_cost_per_contract: float | None = None
    limit_edge: float | None = None
    limit_edge_lcb: float | None = None
    # Diagnostic: which lever actually bounded the recommended size
    # (kelly_budget / position_risk_cap / max_contracts_per_market / ask_size).
    # Lets the dashboard explain why a position is small -- thin edge (kelly),
    # thin book (ask_size), or a configured cap -- rather than guessing.
    binding_constraint: str | None = None
    # Diagnostic split between a qualifying signal and a paper entry. `approved`
    # remains the paper-entry verdict after portfolio/pause/cutoff guards; this
    # field preserves whether the underlying signal qualified before those entry
    # guards were applied.
    signal_approved: bool | None = None
    entry_block_reason: str | None = None

    @property
    def bid(self) -> float:
        return self.yes_bid if self.entry_bid is None else self.entry_bid

    @property
    def ask(self) -> float:
        return self.yes_ask if self.entry_ask is None else self.entry_ask

    @property
    def bid_size(self) -> float:
        return 0.0 if self.entry_bid_size is None else self.entry_bid_size

    @property
    def ask_size(self) -> float:
        if self.entry_ask_size is None:
            return self.yes_ask_size
        return self.entry_ask_size


_MONTH_ABBRS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)
_MONTH_INDEX = {abbr: i + 1 for i, abbr in enumerate(_MONTH_ABBRS)}


def format_event_date_token(target_date: date) -> str:
    """Build the Kalshi ``YYMONDD`` date token with English month abbreviations.

    Kalshi tickers always use uppercase English month abbreviations, so this
    avoids ``strftime('%b')``, which is locale-sensitive and would corrupt the
    ticker under a non-English ``LC_TIME``.
    """

    return f"{target_date.year % 100:02d}{_MONTH_ABBRS[target_date.month - 1]}{target_date.day:02d}"


def target_date_from_event_ticker(event_ticker: str) -> date | None:
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    token = parts[-1].upper()
    if len(token) != 7:
        return None
    month = _MONTH_INDEX.get(token[2:5])
    if month is None:
        return None
    try:
        year = 2000 + int(token[0:2])
        day = int(token[5:7])
        return date(year, month, day)
    except ValueError:
        return None
