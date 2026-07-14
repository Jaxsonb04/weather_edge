from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Iterable

from .config import SFO_TZ, StrategyConfig
from .models import BucketProbability, EnsembleSnapshot, ForecastOutcome, IntradaySnapshot, MarketBin


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def interval_probability_normal(mu: float, sigma: float, lo: float, hi: float) -> float:
    upper = 1.0 if math.isinf(hi) and hi > 0 else normal_cdf(hi, mu, sigma)
    lower = 0.0 if math.isinf(lo) and lo < 0 else normal_cdf(lo, mu, sigma)
    return max(0.0, min(1.0, upper - lower))


def interval_probability_empirical(
    mu: float,
    residuals: list[float],
    lo: float,
    hi: float,
    bandwidth: float = 0.0,
) -> float:
    if not residuals:
        return 0.0
    if bandwidth <= 0.0:
        hits = sum(1 for residual in residuals if lo <= mu + residual < hi)
        return hits / len(residuals)
    # Kernel-smoothed histogram: each residual contributes a small Gaussian
    # mass over [lo, hi] instead of a hard 0/1 hit. A ~35-sample window then
    # stops emitting spurious exact-0.0 tail bins (1/35 discretization), giving
    # smoother, better-calibrated tail mass. normal_cdf handles +/-inf bounds.
    total = 0.0
    for residual in residuals:
        center = mu + residual
        total += normal_cdf(hi, center, bandwidth) - normal_cdf(lo, center, bandwidth)
    return total / len(residuals)


@dataclass(frozen=True)
class ResidualStats:
    residuals: list[float]
    bias: float
    sigma: float
    n: int
    window_f: float


@dataclass(frozen=True)
class IntradayProbabilityModel:
    probabilities: dict[str, float]
    blend_weight: float
    mean_final_high_f: float
    sigma_f: float
    remaining_heat_risk: float | None


class ResidualCalibrator:
    """Convert a point forecast into calibrated temperature-bin probabilities."""

    def __init__(self, outcomes: Iterable[ForecastOutcome], config: StrategyConfig | None = None) -> None:
        self.outcomes = sorted(outcomes, key=lambda row: row.local_date)
        self.config = config or StrategyConfig()
        self.global_residuals = [row.residual_f for row in self.outcomes]
        if len(self.global_residuals) < 30:
            raise ValueError("At least 30 forecast outcomes are required for calibration")
        self._global_stats = self._stats(self.global_residuals, window_f=math.inf)

    @property
    def global_stats(self) -> ResidualStats:
        return self._global_stats

    def conditional_stats(self, predicted_high_f: float) -> ResidualStats:
        for window in (2.0, 3.0, 5.0, 8.0, 12.0):
            residuals = [
                row.residual_f
                for row in self.outcomes
                if abs(row.predicted_high_f - predicted_high_f) <= window
            ]
            if len(residuals) >= self.config.min_conditional_samples:
                return self._stats(residuals, window_f=window)
        return self.global_stats

    def bucket_probabilities(
        self,
        markets: list[MarketBin],
        predicted_high_f: float,
        *,
        source_spread_f: float = 0.0,
        observed_high_f: float | None = None,
        ensemble: EnsembleSnapshot | None = None,
        intraday: IntradaySnapshot | None = None,
        emos_mu_sigma: tuple[float, float] | None = None,
        standard_timezone: tzinfo = SFO_TZ,
    ) -> dict[str, BucketProbability]:
        if observed_high_f is None and intraday is not None:
            observed_high_f = intraday.observed_high_f

        # Resolve the EMOS override up front so the residual conditioning window
        # -- which sizes effective_n and the edge_lcb band below -- is centered
        # where the EMOS distribution actually lives, not the stale blend point.
        emos_active = self.config.emos_distribution_enabled and emos_mu_sigma is not None
        emos_mu = emos_sigma = None
        if emos_active:
            emos_mu, emos_sigma = emos_mu_sigma
            if not (math.isfinite(emos_mu) and math.isfinite(emos_sigma)):
                emos_active = False  # malformed artifact -> keep the residual path

        cond = self.conditional_stats(emos_mu if emos_active else predicted_high_f)
        glob = self.global_stats
        cond_weight = cond.n / (cond.n + self.config.shrinkage_samples)

        sigma = (cond_weight * cond.sigma) + ((1.0 - cond_weight) * glob.sigma)
        bias = (cond_weight * cond.bias) + ((1.0 - cond_weight) * glob.bias)

        # Source disagreement is a real uncertainty signal. Widen gently rather
        # than inventing directional edge from it.
        if source_spread_f > 3.0:
            sigma *= 1.0 + min(0.35, (source_spread_f - 3.0) * 0.04)

        # Flow-dependent sharpening: blend in today's GFS ensemble spread so the
        # model sharpens on calm days and widens on volatile ones, instead of
        # carrying the same static historical sigma every day. Floored at a
        # fraction of the residual sigma because GFS ensembles are
        # under-dispersive and must not be trusted to collapse uncertainty.
        if (
            ensemble is not None
            and ensemble.member_count >= self.config.ensemble_min_members
            and self.config.ensemble_sigma_weight > 0.0
        ):
            sigma_ens = max(0.0, ensemble.station_std_high_f)
            if sigma_ens > 0.0:
                w = self.config.ensemble_sigma_weight
                blended = math.sqrt((1.0 - w) * sigma * sigma + w * sigma_ens * sigma_ens)
                sigma = max(blended, self.config.ensemble_sigma_floor_frac * sigma)

        # EMOS distribution override: drive the normal component directly from the
        # calibrated EMOS Gaussian. Re-center via `bias` so
        # `predicted_high_f + bias == emos_mu` at the per-bin integral below, and
        # replace `sigma`. The conditioning window above is already centered on
        # emos_mu so effective_n / the edge_lcb band reflect the EMOS estimate.
        # Identity (bit-identical) when disabled or no EMOS forecast is available.
        if emos_active:
            bias = emos_mu - predicted_high_f
            sigma = max(emos_sigma, 0.1)  # mirror the residual-path sigma floor (_stats)

        results: dict[str, BucketProbability] = {}
        raw_probs = []
        for market in markets:
            lo, hi = market.continuous_interval()
            p_norm = interval_probability_normal(predicted_high_f + bias, sigma, lo, hi)
            if emos_active:
                # The calibrated EMOS Gaussian IS the weather distribution; the
                # blend's empirical residual shape is fully superseded (no dead compute).
                p_emp = p_norm
            else:
                kernel_bw = self.config.empirical_kernel_bandwidth_f
                p_cond = interval_probability_empirical(predicted_high_f, cond.residuals, lo, hi, kernel_bw)
                p_glob = interval_probability_empirical(predicted_high_f, glob.residuals, lo, hi, kernel_bw)
                p_emp = (cond_weight * p_cond) + ((1.0 - cond_weight) * p_glob)
            p = (self.config.empirical_weight * p_emp) + ((1.0 - self.config.empirical_weight) * p_norm)
            raw_probs.append((market, p, p_emp, p_norm))

        total = sum(prob for _, prob, _, _ in raw_probs) or 1.0
        residual_probs = [
            (market, max(0.0, min(1.0, p_raw / total)), p_emp, p_norm)
            for market, p_raw, p_emp, p_norm in raw_probs
        ]
        observed_high_is_final = bool(intraday.is_complete) if intraday is not None else False
        if observed_high_f is not None:
            residual_probs = _condition_on_observed_high(
                residual_probs, observed_high_f, is_final=observed_high_is_final
            )

        ensemble_probs = _ensemble_bucket_probabilities(
            markets,
            ensemble,
            observed_high_f=observed_high_f,
            min_members=self.config.ensemble_min_members,
            observed_high_is_final=observed_high_is_final,
        )
        weather_probs: list[tuple[MarketBin, float, float, float, float | None]] = []
        for market, residual_p, p_emp, p_norm in residual_probs:
            ensemble_p = ensemble_probs.get(market.ticker) if ensemble_probs else None
            if ensemble_p is None:
                weather_p = residual_p
            else:
                weather_p = (
                    (1.0 - self.config.ensemble_weight) * residual_p
                    + self.config.ensemble_weight * ensemble_p
                )
            weather_probs.append((market, max(0.0, min(1.0, weather_p)), p_emp, p_norm, ensemble_p))

        # Center the intraday model on the EMOS mean when active (predicted_high_f
        # + bias == mu_emos), so the afternoon intraday blend does not drag the
        # EMOS distribution back toward the stale blend point. Identity otherwise.
        intraday_center = (predicted_high_f + bias) if emos_active else predicted_high_f
        intraday_model = _intraday_probability_model(
            markets,
            intraday_center,
            intraday,
            config=self.config,
            standard_timezone=standard_timezone,
        )
        if intraday_model is not None:
            blended_weather_probs: list[tuple[MarketBin, float, float, float, float | None]] = []
            for market, model_p, p_emp, p_norm, ensemble_p in weather_probs:
                intraday_p = intraday_model.probabilities.get(market.ticker)
                if intraday_p is None:
                    blended_weather_probs.append((market, model_p, p_emp, p_norm, ensemble_p))
                    continue
                weight = intraday_model.blend_weight
                blended_p = (1.0 - weight) * model_p + weight * intraday_p
                blended_weather_probs.append(
                    (market, max(0.0, min(1.0, blended_p)), p_emp, p_norm, ensemble_p)
                )
            weather_probs = _normalize_weather_probabilities(blended_weather_probs)

        market_probs = _market_implied_probabilities(markets)
        if observed_high_f is not None and market_probs:
            conditioned_market = _condition_on_observed_high(
                [
                    (market, market_probs.get(market.ticker, 0.0), 0.0, 0.0)
                    for market in markets
                ],
                observed_high_f,
                is_final=observed_high_is_final,
            )
            market_probs = {market.ticker: p for market, p, _, _ in conditioned_market}
        effective_n = (cond_weight * cond.n) + ((1.0 - cond_weight) * glob.n)
        # The lower-confidence band must reflect the precision of the estimate
        # that actually carries the conditioning, which rests on the conditional
        # window (cond.n), not the much larger global count that dominates the
        # blended effective_n. Using effective_n understated the SE ~3x exactly
        # when the conditional window was sparse, weakening the edge_lcb gate
        # that is the primary real-money defense. Cap the SE sample size at the
        # conditional support so the band widens when conditioning is thin.
        se_sample_n = min(cond.n, effective_n)
        model_risk_penalty = min(0.08, max(0.0, source_spread_f - 3.0) * 0.0075)
        residual_by_ticker = {market.ticker: p for market, p, _, _ in residual_probs}
        for market, model_p, p_emp, p_norm, ensemble_p in weather_probs:
            intraday_p = None
            remaining_heat_risk = None
            if intraday_model is not None:
                intraday_p = intraday_model.probabilities.get(market.ticker)
                remaining_heat_risk = intraday_model.remaining_heat_risk
            market_p = market_probs.get(market.ticker)
            if market_p is None:
                p = model_p
                disagreement_penalty = 0.0
            else:
                model_weight = _model_weight(
                    source_spread_f,
                    market=market,
                    config=self.config,
                )
                p = (model_weight * model_p) + ((1.0 - model_weight) * market_p)
                disagreement_penalty = abs(model_p - market_p) * self.config.market_disagreement_lcb_penalty
            residual_p = residual_by_ticker.get(market.ticker, model_p)
            ensemble_disagreement_penalty = 0.0
            if ensemble_p is not None:
                ensemble_disagreement_penalty = (
                    abs(residual_p - ensemble_p) * self.config.ensemble_disagreement_lcb_penalty
                )
            p = max(0.0, min(1.0, p))
            standard_error = math.sqrt(max(0.0, p * (1.0 - p)) / max(1.0, se_sample_n))
            lower_confidence = max(
                0.0,
                p
                - self.config.confidence_z * standard_error
                - model_risk_penalty
                - disagreement_penalty
                - ensemble_disagreement_penalty,
            )
            results[market.ticker] = BucketProbability(
                ticker=market.ticker,
                label=market.yes_sub_title,
                probability=p,
                lower_confidence=lower_confidence,
                empirical_probability=p_emp,
                normal_probability=p_norm,
                effective_n=effective_n,
                residual_probability=residual_p,
                ensemble_probability=ensemble_p,
                model_probability=model_p,
                market_probability=market_p,
                observed_high_f=observed_high_f,
                intraday_probability=intraday_p,
                remaining_heat_risk=remaining_heat_risk,
                observed_high_is_final=(
                    observed_high_is_final if observed_high_f is not None else None
                ),
            )
        return results

    @staticmethod
    def _stats(residuals: list[float], window_f: float) -> ResidualStats:
        bias = statistics.fmean(residuals)
        sigma = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
        return ResidualStats(
            residuals=list(residuals),
            bias=bias,
            sigma=max(0.1, sigma),
            n=len(residuals),
            window_f=window_f,
        )


# A raw nonfinal station maximum is NOT the official integer daily-climate
# value the market settles on: METAR temperatures are rounded conversions,
# the official report aggregates different sensors/windows, and revisions
# happen. Production evidence (audit MD-01, order 188): Philadelphia's raw
# station max read 87.8°F while the final official integer high was 87°F, so
# hard-zeroing the 86-87°F bin created false settlement certainty and lost
# $11.63. Model the raw-to-official mapping error as a Gaussian with this
# sigma until the MD-02 challenger calibrates a per-station replacement.
NONFINAL_OBSERVED_HIGH_SIGMA_F = 0.6
# Bins this unlikely to remain reachable are treated as exactly excluded, so
# far-below bins still price to zero instead of accumulating dust mass.
_NONFINAL_FEASIBILITY_CUTOFF = 1e-3


def _nonfinal_bin_feasibility(observed_high_f: float, hi: float) -> float:
    """P(the official integer high can still land at/below ``hi``).

    Applies only while the observation is nonfinal: the raw running maximum
    ``observed_high_f`` maps to the official settlement value with error
    ~N(0, NONFINAL_OBSERVED_HIGH_SIGMA_F). Bins whose upper edge sits above
    the raw maximum are always feasible (feasibility 1.0).
    """

    if hi > observed_high_f:
        return 1.0
    feasibility = normal_cdf(hi, observed_high_f, NONFINAL_OBSERVED_HIGH_SIGMA_F)
    return 0.0 if feasibility < _NONFINAL_FEASIBILITY_CUTOFF else feasibility


def _condition_on_observed_high(
    probabilities: list[tuple[MarketBin, float, float, float]],
    observed_high_f: float,
    *,
    is_final: bool = False,
) -> list[tuple[MarketBin, float, float, float]]:
    """Condition bin probabilities on the observed high so far.

    With ``is_final`` truth (a complete official daily report) the exclusion
    is exact, including the point-mass shortcut for an unbounded top bin.
    With a nonfinal raw observation, exact 0/1 posteriors are forbidden
    (audit MD-01): bins at/below the raw maximum are damped by the
    observation-to-official feasibility instead of hard-zeroed, and no point
    mass is created.
    """

    certain_ticker = None
    filtered: list[tuple[MarketBin, float, float, float]] = []
    for market, p, p_emp, p_norm in probabilities:
        lo, hi = market.continuous_interval()
        if not is_final:
            feasibility = _nonfinal_bin_feasibility(observed_high_f, hi)
            filtered.append((market, p * feasibility, p_emp, p_norm))
            continue
        if observed_high_f >= hi:
            filtered.append((market, 0.0, p_emp, p_norm))
        elif observed_high_f >= lo and math.isinf(hi) and hi > 0:
            certain_ticker = market.ticker
            filtered.append((market, 1.0, p_emp, p_norm))
        else:
            filtered.append((market, p, p_emp, p_norm))

    if certain_ticker is not None:
        return [
            (market, 1.0 if market.ticker == certain_ticker else 0.0, p_emp, p_norm)
            for market, _, p_emp, p_norm in filtered
        ]

    total = sum(p for _, p, _, _ in filtered)
    if total <= 0:
        return filtered
    return [(market, p / total, p_emp, p_norm) for market, p, p_emp, p_norm in filtered]


def _normalize_weather_probabilities(
    probabilities: list[tuple[MarketBin, float, float, float, float | None]],
) -> list[tuple[MarketBin, float, float, float, float | None]]:
    if not probabilities:
        return probabilities
    total = sum(p for _, p, _, _, _ in probabilities)
    if total <= 0:
        # Zero total mass means the intraday blend lost all information across
        # the offered bins (e.g. an intraday point-mass landing entirely outside
        # them). Returning the un-normalized list silently zeroed every bucket's
        # probability and edge -- an invisible kill of every signal. Fall back to
        # a uniform prior so the vector still sums to 1 and downstream model_p
        # math stays well-defined. Uniform is the max-entropy, least-biased
        # stand-in and, after the market blend and LCB penalties, is unlikely to
        # clear edge_lcb, so it produces no trade rather than a corrupted one.
        uniform = 1.0 / len(probabilities)
        return [
            (market, uniform, p_emp, p_norm, ensemble_p)
            for market, _p, p_emp, p_norm, ensemble_p in probabilities
        ]
    return [
        (market, p / total, p_emp, p_norm, ensemble_p)
        for market, p, p_emp, p_norm, ensemble_p in probabilities
    ]


def _intraday_probability_model(
    markets: list[MarketBin],
    predicted_high_f: float,
    intraday: IntradaySnapshot | None,
    *,
    config: StrategyConfig,
    standard_timezone: tzinfo = SFO_TZ,
) -> IntradayProbabilityModel | None:
    if intraday is None or intraday.observed_high_f is None:
        return None

    observed_high_f = intraday.observed_high_f
    if intraday.latest_temp_f is not None:
        observed_high_f = max(observed_high_f, intraday.latest_temp_f)

    if intraday.is_complete:
        return IntradayProbabilityModel(
            probabilities=_point_mass_probabilities(markets, observed_high_f),
            blend_weight=1.0,
            mean_final_high_f=observed_high_f,
            sigma_f=config.intraday_min_sigma_f,
            remaining_heat_risk=0.0,
        )

    local_hour = _local_decimal_hour(intraday.latest_observed_at, standard_timezone)
    climatology_upside_f = _climatological_remaining_heat_f(local_hour)
    # Center on the forecast-based estimates (the model's actual expectation for
    # the day's high), with observed-so-far as a hard lower bound. The old code
    # additionally lifted the mean by 0.35*climatology_upside, which stacked a
    # directional push on top of a right-truncated normal and over-priced
    # high-temp tails (modeled 8.7% vs realized 1.9%). The remaining-rise
    # uncertainty now lives only in sigma, not in a mean lift.
    mean_final_high_f = max(observed_high_f, predicted_high_f)
    if intraday.remaining_forecast_high_f is not None:
        mean_final_high_f = max(mean_final_high_f, intraday.remaining_forecast_high_f)

    sigma_f = _intraday_sigma_f(local_hour, climatology_upside_f, config)
    probabilities = _conditioned_normal_final_high_probabilities(
        markets,
        observed_high_f=observed_high_f,
        mean_final_high_f=mean_final_high_f,
        sigma_f=sigma_f,
    )
    remaining_heat_risk = _remaining_heat_risk(
        markets,
        observed_high_f=observed_high_f,
        mean_final_high_f=mean_final_high_f,
        sigma_f=sigma_f,
    )
    boundary_gap_f = _upper_boundary_gap(markets, observed_high_f)

    blend_weight = _intraday_blend_weight(local_hour, config)
    if (
        remaining_heat_risk is not None
        and boundary_gap_f is not None
        and boundary_gap_f <= config.intraday_boundary_watch_f
        and remaining_heat_risk >= 0.05
    ):
        blend_weight += config.intraday_boundary_weight_boost
    elif remaining_heat_risk is not None and remaining_heat_risk >= 0.20:
        blend_weight += config.intraday_boundary_weight_boost
    blend_weight = max(0.0, min(1.0, blend_weight))

    return IntradayProbabilityModel(
        probabilities=probabilities,
        blend_weight=blend_weight,
        mean_final_high_f=mean_final_high_f,
        sigma_f=sigma_f,
        remaining_heat_risk=remaining_heat_risk,
    )


def _point_mass_probabilities(markets: list[MarketBin], observed_high_f: float) -> dict[str, float]:
    rows = []
    for market in markets:
        lo, hi = market.continuous_interval()
        rows.append((market, 1.0 if lo <= observed_high_f < hi else 0.0))
    total = sum(p for _, p in rows)
    if total <= 0:
        return {}
    return {market.ticker: p / total for market, p in rows}


def _conditioned_normal_final_high_probabilities(
    markets: list[MarketBin],
    *,
    observed_high_f: float,
    mean_final_high_f: float,
    sigma_f: float,
) -> dict[str, float]:
    # This path only runs on NONFINAL observations (final truth short-circuits
    # to the point mass), so the raw running maximum is not an exact floor for
    # the official integer settlement value (audit MD-01). Relax the
    # truncation floor by two observation-error sigmas so integer-report
    # boundary bins just below the raw maximum keep reachable mass.
    floor_f = observed_high_f - 2.0 * NONFINAL_OBSERVED_HIGH_SIGMA_F
    denominator = max(1e-9, 1.0 - normal_cdf(floor_f, mean_final_high_f, sigma_f))
    rows: list[tuple[MarketBin, float]] = []
    for market in markets:
        lo, hi = market.continuous_interval()
        if hi <= floor_f:
            probability = 0.0
        else:
            probability = interval_probability_normal(
                mean_final_high_f,
                sigma_f,
                max(lo, floor_f),
                hi,
            ) / denominator
        rows.append((market, probability))

    total = sum(p for _, p in rows)
    if total <= 0:
        return {}
    return {market.ticker: max(0.0, min(1.0, p / total)) for market, p in rows}


def _remaining_heat_risk(
    markets: list[MarketBin],
    *,
    observed_high_f: float,
    mean_final_high_f: float,
    sigma_f: float,
) -> float | None:
    current_upper = None
    for market in markets:
        lo, hi = market.continuous_interval()
        if lo <= observed_high_f < hi and math.isfinite(hi):
            current_upper = hi
            break
    if current_upper is None:
        return None
    if observed_high_f >= current_upper:
        return 1.0
    denominator = max(1e-9, 1.0 - normal_cdf(observed_high_f, mean_final_high_f, sigma_f))
    survival = 1.0 - normal_cdf(current_upper, mean_final_high_f, sigma_f)
    return max(0.0, min(1.0, survival / denominator))


def _upper_boundary_gap(markets: list[MarketBin], observed_high_f: float) -> float | None:
    for market in markets:
        lo, hi = market.continuous_interval()
        if lo <= observed_high_f < hi and math.isfinite(hi):
            return max(0.0, hi - observed_high_f)
    return None


def _local_decimal_hour(
    observed_at: str | None,
    standard_timezone: tzinfo = SFO_TZ,
) -> float | None:
    if not observed_at:
        return None
    try:
        observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    local = observed_dt.astimezone(standard_timezone)
    return local.hour + local.minute / 60.0


def _climatological_remaining_heat_f(local_hour: float | None) -> float:
    # Pre-dawn the overnight "high so far" says almost nothing about the
    # afternoon peak: nearly the full diurnal range is still ahead. The
    # 2026-06-10 loss came from treating a 2:36am observed high as if the
    # day were mostly resolved (intraday p(>=79F) collapsed to 0.008 while
    # the day settled at 79F).
    if local_hour is None:
        return 1.2
    if local_hour < 6.0:
        return 12.0
    if local_hour < 8.0:
        return 8.0
    if local_hour < 10.0:
        return 4.5
    if local_hour < 12.0:
        return 2.2
    if local_hour < 14.0:
        return 1.4
    if local_hour < 16.0:
        return 0.8
    if local_hour < 18.0:
        return 0.35
    return 0.15


def _intraday_sigma_f(
    local_hour: float | None,
    climatology_upside_f: float,
    config: StrategyConfig,
) -> float:
    if local_hour is None:
        base = 0.95
    elif local_hour < 6.0:
        base = 3.0
    elif local_hour < 8.0:
        base = 2.2
    elif local_hour < 10.0:
        base = 1.35
    elif local_hour < 12.0:
        base = 1.10
    elif local_hour < 14.0:
        base = 0.90
    elif local_hour < 16.0:
        base = 0.70
    elif local_hour < 18.0:
        base = 0.45
    else:
        base = 0.30
    sigma = max(base, 0.45 * climatology_upside_f)
    return max(config.intraday_min_sigma_f, min(config.intraday_max_sigma_f, sigma))


def _intraday_blend_weight(local_hour: float | None, config: StrategyConfig) -> float:
    if local_hour is None:
        base = 0.45
    elif local_hour < 6.0:
        base = 0.10
    elif local_hour < 8.0:
        base = 0.20
    elif local_hour < 10.0:
        base = 0.30
    elif local_hour < 12.0:
        base = 0.40
    elif local_hour < 14.0:
        base = 0.55
    elif local_hour < 16.0:
        base = 0.65
    elif local_hour < 18.0:
        base = 0.75
    else:
        base = 0.90
    return min(config.intraday_probability_weight, base)


def market_implied_probabilities(markets: list[MarketBin]) -> dict[str, float]:
    """Public de-vigged ladder distribution (ticker -> probability, sums to ~1).

    The single source of truth for "what the market implies", shared by the
    per-bin market prior here and the ladder-level consensus in ``consensus``.
    """

    return _market_implied_probabilities(markets)


def market_implied_yes_value(market: MarketBin) -> float | None:
    """Public single-bin implied YES probability (pre-normalization), or None."""

    return _market_implied_yes_value(market)


def _market_implied_probabilities(markets: list[MarketBin]) -> dict[str, float]:
    raw: dict[str, float] = {}
    for market in markets:
        if market.status != "active":
            continue
        value = _market_implied_yes_value(market)
        if value is not None:
            raw[market.ticker] = max(0.0, value)
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {ticker: value / total for ticker, value in raw.items()}


def _market_implied_yes_value(market: MarketBin) -> float | None:
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []
    if 0.0 < market.yes_bid < 1.0:
        lower_bounds.append(market.yes_bid)
    if 0.0 < market.yes_ask < 1.0:
        upper_bounds.append(market.yes_ask)
    if 0.0 < market.no_ask < 1.0:
        lower_bounds.append(1.0 - market.no_ask)
    if 0.0 < market.no_bid < 1.0:
        upper_bounds.append(1.0 - market.no_bid)

    if not lower_bounds and not upper_bounds:
        return None
    lower = max(lower_bounds) if lower_bounds else None
    upper = min(upper_bounds) if upper_bounds else None
    if lower is not None and upper is not None:
        return _clamp_probability((lower + upper) / 2.0)
    # One-sided book: assume the true value is uniform between the known bound
    # and the far edge. Ask-only -> midpoint of [0, ask]; bid-only -> midpoint
    # of [bid, 1] (the previous code returned the bare bid, an asymmetry that
    # understated the implied probability on bid-only books).
    if upper is not None:
        return _clamp_probability(upper / 2.0)
    return _clamp_probability((lower + 1.0) / 2.0)


def _clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ensemble_bucket_probabilities(
    markets: list[MarketBin],
    ensemble: EnsembleSnapshot | None,
    *,
    observed_high_f: float | None,
    min_members: int,
    observed_high_is_final: bool = False,
) -> dict[str, float]:
    if ensemble is None or ensemble.member_count < min_members:
        return {}
    rows: list[tuple[MarketBin, float, float, float]] = []
    members = ensemble.station_member_highs_f
    for market in markets:
        lo, hi = market.continuous_interval()
        hits = sum(1 for value in members if lo <= value < hi)
        rows.append((market, hits / len(members), 0.0, 0.0))

    total = sum(p for _, p, _, _ in rows)
    if total <= 0:
        return {}
    rows = [(market, p / total, 0.0, 0.0) for market, p, _, _ in rows]
    if observed_high_f is not None:
        rows = _condition_on_observed_high(
            rows, observed_high_f, is_final=observed_high_is_final
        )
    return {market.ticker: p for market, p, _, _ in rows}


def _model_weight(
    source_spread_f: float,
    *,
    market: MarketBin | None,
    config: StrategyConfig,
) -> float:
    if market is None:
        return 1.0
    # The "huge consideration" anchor: when enabled, the market gets a heavier
    # base voice (anchor_weight, e.g. 0.60 vs 0.45) and a lower model floor, so a
    # confident, liquid ladder pulls the posterior harder toward the crowd. The
    # reliability scaling and the model-weight floor still bind, so a thin/wide
    # book never dominates and the model is never fully silenced (the edge lives
    # in its residual disagreement). Default off on live pending a walk-forward
    # backtest; on for research to collect validation samples.
    if config.market_consensus_anchor_enabled:
        base_weight = config.market_consensus_anchor_weight
        min_model_weight = config.market_consensus_anchor_min_model_weight
    else:
        base_weight = config.market_prior_weight
        min_model_weight = config.min_model_weight
    market_weight = base_weight
    if source_spread_f > 3.0:
        market_weight += (source_spread_f - 3.0) * config.source_spread_market_weight_per_f
    market_weight *= _market_prior_reliability(market, config)
    market_weight = min(1.0 - min_model_weight, max(0.0, market_weight))
    return 1.0 - market_weight


def _market_prior_reliability(market: MarketBin, config: StrategyConfig) -> float:
    spreads = [
        spread
        for spread in (market.spread, market.no_spread)
        if spread > 0.0
    ]
    spread = min(spreads) if spreads else config.market_prior_wide_spread
    if spread <= config.market_prior_tight_spread:
        spread_score = 1.0
    elif spread >= config.market_prior_wide_spread:
        spread_score = config.market_prior_min_reliability
    else:
        span = config.market_prior_wide_spread - config.market_prior_tight_spread
        spread_score = 1.0 - (spread - config.market_prior_tight_spread) / max(span, 1e-9)
        spread_score = max(config.market_prior_min_reliability, spread_score)

    depth = max(
        market.yes_bid_size,
        market.yes_ask_size,
        market.no_bid_size,
        market.no_ask_size,
    )
    depth_score = max(
        config.market_prior_min_reliability,
        min(1.0, depth / max(config.market_prior_full_depth, 1e-9)),
    )

    consistency_score = 1.0
    reciprocal_pairs = []
    if 0.0 < market.yes_bid < 1.0 and 0.0 < market.no_ask < 1.0:
        reciprocal_pairs.append(abs(market.yes_bid - (1.0 - market.no_ask)))
    if 0.0 < market.yes_ask < 1.0 and 0.0 < market.no_bid < 1.0:
        reciprocal_pairs.append(abs(market.yes_ask - (1.0 - market.no_bid)))
    if reciprocal_pairs and max(reciprocal_pairs) > max(config.market_prior_wide_spread, 0.01):
        consistency_score = 0.5

    reliability = spread_score * depth_score * consistency_score
    return max(config.market_prior_min_reliability, min(1.0, reliability))
