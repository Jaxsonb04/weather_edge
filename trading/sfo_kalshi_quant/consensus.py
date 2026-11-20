"""The Kalshi forecast for the SFO daily-high market, rebuilt from the ladder.

Kalshi prints a headline "forecast" on each temperature market (e.g. "70.7
forecast") -- the market-implied expected high. That number is NOT a field in
the public API; Kalshi derives it from the prices, volume-weighting toward the
last traded price in each bin. So this module reconstructs the same number from
the prices the API does return: per bin we take the last trade when present
(else the de-vigged two-sided bid/ask mid), normalize across the ladder into a
distribution, and read its expected value, mode, spread, and percentiles. The
result -- ``implied_high_f`` -- is the Kalshi forecast.

It is surfaced next to the weather model and, when enabled, anchors the traded
posterior and drives the "don't bet hard against a confident, liquid market"
guard, so the engine reacts to the same forecast the market is trading on.

Pure functions: no I/O, no global state.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import MarketBin
from .probability import market_implied_yes_value

# Fallback bin width (degrees F) when no finite-width bin exists to measure. The
# SFO ladder is built from 2F "between" bins, so 2.0 is the natural default and
# is only ever reached on a degenerate all-tails ladder.
_DEFAULT_BIN_WIDTH_F = 2.0


@dataclass(frozen=True)
class ConsensusBin:
    """One ladder bin's market-implied probability and its temperature span."""

    ticker: str
    label: str
    lo_f: float  # continuous interval lower bound (may be -inf for a low tail)
    hi_f: float  # continuous interval upper bound (may be +inf for a high tail)
    center_f: float  # finite representative temperature used for moments
    implied_probability: float  # de-vigged, ladder-normalized market probability


@dataclass(frozen=True)
class MarketConsensus:
    """The market's consensus forecast distilled from the full bin ladder.

    ``available`` is False when the ladder carries no usable two-sided prices
    (e.g. an unlisted event, or a fallback paper ladder with no real book). All
    optional fields are None in that case so callers can render "n/a" cleanly.
    """

    available: bool
    implied_high_f: float | None  # THE Kalshi forecast: probability-weighted
    # mean of bin centers, priced last-trade-first to match Kalshi's headline
    modal_bin_ticker: str | None
    modal_bin_label: str | None
    modal_probability: float
    implied_stdev_f: float | None  # market confidence (lower = tighter forecast)
    p10_f: float | None
    p25_f: float | None
    median_f: float | None  # P50
    p75_f: float | None
    p90_f: float | None
    overround: float  # sum(raw implied mids) - 1.0; ~0 for a balanced ladder,
    # negative when the book is thin/incomplete (mass well under 1)
    liquid_bin_count: int  # bins with a genuine two-sided quote
    bins: tuple[ConsensusBin, ...]

    def gap_to_forecast_f(self, forecast_high_f: float | None) -> float | None:
        """Signed model-minus-market high gap (our forecast - market consensus)."""

        if forecast_high_f is None or self.implied_high_f is None:
            return None
        return forecast_high_f - self.implied_high_f


_EMPTY_CONSENSUS = MarketConsensus(
    available=False,
    implied_high_f=None,
    modal_bin_ticker=None,
    modal_bin_label=None,
    modal_probability=0.0,
    implied_stdev_f=None,
    p10_f=None,
    p25_f=None,
    median_f=None,
    p75_f=None,
    p90_f=None,
    overround=0.0,
    liquid_bin_count=0,
    bins=(),
)


def build_market_consensus(markets: list[MarketBin]) -> MarketConsensus:
    """Reconstruct the Kalshi forecast from a bin ladder.

    Each active bin is priced last-trade-first (falling back to the de-vigged
    bid/ask mid), normalized across the ladder into a distribution, then mapped
    to finite bin-center temperatures (open tails extrapolated one bin width) so
    we can read the expected high (the Kalshi forecast), mode, spread, and
    percentiles.
    """

    width = _typical_bin_width_f(markets)

    # Per-bin "Kalshi price": the last trade when present (Kalshi volume-weights
    # its headline forecast toward trades), else the de-vigged two-sided bid/ask
    # mid. Active-only, so a lingering quote on a closed bin cannot bias the
    # forecast or inflate the overround the guard reads.
    prices = {
        market.ticker: max(0.0, price)
        for market in markets
        if market.status == "active"
        for price in (_consensus_price(market),)
        if price is not None
    }
    raw_total = sum(prices.values())
    if raw_total <= 0:
        return _EMPTY_CONSENSUS
    implied = {ticker: value / raw_total for ticker, value in prices.items()}
    # Raw (pre-normalization) mass measures the book's balance: a tight, complete
    # ladder sums near 1.0; a thin/incomplete one falls short. A large |overround|
    # flags a book the guard should not defer to.
    overround = raw_total - 1.0

    bins: list[ConsensusBin] = []
    for market in markets:
        probability = implied.get(market.ticker, 0.0)
        lo_f, hi_f = market.continuous_interval()
        center_f = _bin_center_f(lo_f, hi_f, width)
        bins.append(
            ConsensusBin(
                ticker=market.ticker,
                label=market.yes_sub_title or market.ticker,
                lo_f=lo_f,
                hi_f=hi_f,
                center_f=center_f,
                implied_probability=probability,
            )
        )

    scored = [b for b in bins if b.implied_probability > 0.0 and math.isfinite(b.center_f)]
    if not scored:
        return _EMPTY_CONSENSUS

    # Renormalize the moments over exactly the mass we can place on a finite
    # temperature. A malformed open-ended bin with no finite bound yields a nan
    # center and is dropped from ``scored``; dividing by the scored mass keeps
    # the mean/variance unbiased instead of silently shrinking toward zero.
    scored_mass = sum(b.implied_probability for b in scored)
    implied_high_f = sum(b.implied_probability * b.center_f for b in scored) / scored_mass
    variance = (
        sum(b.implied_probability * (b.center_f - implied_high_f) ** 2 for b in scored)
        / scored_mass
    )
    implied_stdev_f = math.sqrt(max(0.0, variance))

    modal = max(scored, key=lambda b: b.implied_probability)

    percentiles = _ladder_percentiles(bins, width, (0.10, 0.25, 0.50, 0.75, 0.90))

    liquid_bin_count = sum(
        1 for market in markets if market.status == "active" and _is_two_sided(market)
    )

    return MarketConsensus(
        available=True,
        implied_high_f=implied_high_f,
        modal_bin_ticker=modal.ticker,
        modal_bin_label=modal.label,
        modal_probability=modal.implied_probability,
        implied_stdev_f=implied_stdev_f,
        p10_f=percentiles[0.10],
        p25_f=percentiles[0.25],
        median_f=percentiles[0.50],
        p75_f=percentiles[0.75],
        p90_f=percentiles[0.90],
        overround=overround,
        liquid_bin_count=liquid_bin_count,
        bins=tuple(bins),
    )


# Open-ended tail brackets are centred this far beyond their open edge when the
# ladder is reduced to one published number (see ladder_mean).
_TAIL_OFFSET_F = 3.0
_BIN_VARIANCE_F2 = 1.0 / 3.0  # within-bracket variance of a 2F bin
_MIN_LADDER_SD_F = 0.5
_OBSERVATION_FRESH = timedelta(hours=2)
# The settlement-day observed high is a hard floor on the day's high (half a
# degree of rounding slack, as the intraday update uses).
_OBSERVED_FLOOR_SLACK_F = 0.5


def ladder_mean(markets: list[MarketBin]) -> tuple[float, float] | None:
    """Market-implied (mean, sd) of the daily high, or None for an unusable ladder.

    Prices are the two-sided mid (ask/2 ask-only, (bid+1)/2 bid-only) normalised
    over the active ladder. Separate from ``build_market_consensus`` on purpose:
    that feeds recorded audit fields, so its tail centres must not move.
    Requires both open-ended tails active and at least one priced bracket, since
    a partial ladder biases the mean toward the brackets that happen to be listed.
    """

    points: list[tuple[float, float]] = []
    has_low = has_high = False
    for market in markets:
        if market.status != "active":
            continue
        lo_f, hi_f = market.continuous_interval()
        lo_open, hi_open = not math.isfinite(lo_f), not math.isfinite(hi_f)
        if lo_open and hi_open:
            continue  # malformed bracket with no finite edge
        has_low |= lo_open
        has_high |= hi_open
        price = market_implied_yes_value(market)
        if price is None or price <= 0.0:
            continue
        if lo_open:
            center = hi_f - _TAIL_OFFSET_F
        elif hi_open:
            center = lo_f + _TAIL_OFFSET_F
        else:
            center = (lo_f + hi_f) / 2.0
        points.append((center, price))
    total = sum(price for _, price in points)
    if not (has_low and has_high) or total <= 0.0:
        return None
    mean = sum(c * p for c, p in points) / total
    variance = sum(p * (c - mean) ** 2 for c, p in points) / total
    return mean, math.sqrt(max(variance - _BIN_VARIANCE_F2, _MIN_LADDER_SD_F**2))


def apply_market_headline(
    forecast: dict,
    ladder: tuple[float, float] | None,
    *,
    intraday: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Publish the market mean as the headline, keeping the model value beside it.

    ``forecast`` is a published forecast dict whose ``predicted_high_f`` is the
    model (already intraday-updated). On the settlement day the market mean is
    floored at the observed high so far, but only from a fresh observation. The
    observed-high fold stays on the model value alone, so nothing is applied twice.
    """

    out = {
        **forecast,
        "model_high_f": forecast["predicted_high_f"],
        "model_sigma_f": forecast.get("sigma_f"),
        "market_implied_high_f": None,
        "forecast_source": "model",
    }
    if ladder is None:
        return out
    mean, sd = ladder
    headline = mean
    observed = (intraday or {}).get("observed_high_f")
    if observed is not None and _observation_is_fresh(intraday, now):
        headline = max(headline, float(observed) - _OBSERVED_FLOOR_SLACK_F)
    out.update(
        predicted_high_f=round(headline, 2),
        sigma_f=round(sd, 2),
        market_implied_high_f=round(mean, 2),
        forecast_source="market",
    )
    return out


def _observation_is_fresh(intraday: dict | None, now: datetime | None) -> bool:
    try:
        seen = datetime.fromisoformat(str(intraday["latest_observed_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - seen <= _OBSERVATION_FRESH


def _is_two_sided(market: MarketBin) -> bool:
    """A genuine two-sided quote on either the YES or NO book."""

    yes_two_sided = market.yes_bid > 0.0 and market.yes_ask < 1.0
    no_two_sided = market.no_bid > 0.0 and market.no_ask < 1.0
    return yes_two_sided or no_two_sided


def _consensus_price(market: MarketBin) -> float | None:
    """Per-bin YES price the Kalshi forecast is built from.

    Kalshi volume-weights its headline forecast toward executed trades, so we
    prefer the bin's last traded price (``last_price_dollars``) when it is a real
    in-(0,1) probability. With no trade yet, fall back to the de-vigged two-sided
    bid/ask mid (``market_implied_yes_value``). Returns None when the bin has
    neither -- it then carries no mass in the forecast.
    """

    last = _as_optional_price(market.raw.get("last_price_dollars"))
    if last is not None and 0.0 < last < 1.0:
        return last
    return market_implied_yes_value(market)


def _as_optional_price(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _typical_bin_width_f(markets: list[MarketBin]) -> float:
    widths: list[float] = []
    for market in markets:
        lo_f, hi_f = market.continuous_interval()
        if math.isfinite(lo_f) and math.isfinite(hi_f) and hi_f > lo_f:
            widths.append(hi_f - lo_f)
    if not widths:
        return _DEFAULT_BIN_WIDTH_F
    return statistics.median(widths)


def _bin_center_f(lo_f: float, hi_f: float, width: float) -> float:
    """A finite representative temperature for a bin.

    Interior bins use their midpoint. Open tails (``less`` / ``greater``) have no
    finite outer edge, so we place their mass one typical bin-width inside the
    known boundary -- the bulk of an adjacent-to-the-body tail sits just past the
    threshold, not far out where it would distort the implied mean.
    """

    lo_finite = math.isfinite(lo_f)
    hi_finite = math.isfinite(hi_f)
    if lo_finite and hi_finite:
        return (lo_f + hi_f) / 2.0
    if hi_finite:  # low tail: (-inf, hi)
        return hi_f - width / 2.0
    if lo_finite:  # high tail: (lo, +inf)
        return lo_f + width / 2.0
    return math.nan


def _effective_span(lo_f: float, hi_f: float, width: float) -> tuple[float, float]:
    """Finite [lo, hi] span used to model a bin as uniform mass for percentiles."""

    lo_finite = math.isfinite(lo_f)
    hi_finite = math.isfinite(hi_f)
    if lo_finite and hi_finite:
        return lo_f, hi_f
    if hi_finite:  # low tail
        return hi_f - width, hi_f
    if lo_finite:  # high tail
        return lo_f, lo_f + width
    return math.nan, math.nan


def _ladder_percentiles(
    bins: list[ConsensusBin],
    width: float,
    quantiles: tuple[float, ...],
) -> dict[float, float | None]:
    """Inverse-CDF percentiles treating each bin as uniform over its span.

    Bins are ordered by temperature and each contributes its probability mass
    uniformly across its (finite) span, so the CDF is piecewise-linear and a
    quantile interpolates within the bin that straddles it. This yields smooth
    P10/P50/P90 estimates that respect bin widths rather than snapping to edges.
    """

    spans: list[tuple[float, float, float]] = []  # (lo, hi, probability)
    for b in bins:
        if b.implied_probability <= 0.0:
            continue
        lo, hi = _effective_span(b.lo_f, b.hi_f, width)
        if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            continue
        spans.append((lo, hi, b.implied_probability))

    result: dict[float, float | None] = {q: None for q in quantiles}
    if not spans:
        return result

    spans.sort(key=lambda row: row[0])
    total = sum(row[2] for row in spans)
    if total <= 0:
        return result

    for q in quantiles:
        target = q * total
        cumulative = 0.0
        value = spans[-1][1]  # default to the top edge if rounding overshoots
        for lo, hi, mass in spans:
            if cumulative + mass >= target:
                fraction = (target - cumulative) / mass if mass > 0 else 0.0
                value = lo + fraction * (hi - lo)
                break
            cumulative += mass
        result[q] = value
    return result
