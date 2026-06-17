from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SFO_TZ = ZoneInfo("America/Los_Angeles")
SERIES_TICKER = "KXHIGHTSFO"


def _default_forecaster_root() -> Path:
    return PROJECT_ROOT.parent / "forecaster"


DEFAULT_FORECASTER_ROOT = Path(os.getenv("SFO_FORECASTER_ROOT", str(_default_forecaster_root())))
DEFAULT_DB_PATH = Path(os.getenv("SFO_KALSHI_DB", PROJECT_ROOT / "data" / "paper_trading.db"))


@dataclass(frozen=True)
class StrategyConfig:
    """Conservative gate values for paper trading.

    `StrategyConfig()` stays strict so tests and explicit conservative runs are
    stable. The CLI uses `strategy_config_for_profile()` so the paper-research
    default can collect more examples without changing these baseline gates.
    """

    paper_bankroll: float = float(os.getenv("PAPER_BANKROLL", "1000"))
    min_edge: float = 0.03
    min_edge_lcb: float = 0.00
    max_spread: float = 0.06
    max_spread_fraction_of_cost: float = 0.35
    min_yes_bid: float = 0.01
    min_yes_bid_size: float = 1.0
    # Entry-liquidity floor mirroring min_yes_bid_size on the exit side. Live
    # market payloads carry top-of-book ask depth (yes_ask_size_fp); a zero or
    # thin displayed ask means the assumed entry price cannot actually be
    # filled, so sizing off it would overstate reachable size and PnL.
    min_ask_size: float = 1.0
    max_model_market_gap: float = 0.12
    min_posterior_probability: float = 0.06
    fractional_kelly: float = 0.15
    kelly_lcb_weight: float = 1.0
    max_position_risk_pct: float = 0.01
    max_event_risk_pct: float = 0.03
    # When True, Kelly and the percentage risk caps size against live paper
    # equity (starting bankroll + realized PnL) instead of the frozen notional,
    # so sizing compounds correctly after wins/losses. Off by default to keep
    # paper runs reproducible; enable it for a real-money-shaped run.
    size_against_live_equity: bool = False
    max_target_exposure_pct: float = 0.05
    max_entries_per_market_side: int = 1
    max_contracts_per_market: float = 25.0
    max_forecast_age_hours: float = 30.0
    allow_fractional_contracts: bool = False
    # Round integer contracts to nearest instead of truncating with int().
    # Truncation systematically under-sizes (raw 1.7 -> 1 is a 41% stake cut),
    # making the paper journal a pessimistic record of what would actually be
    # risked. Off on the frozen conservative baseline for reproducible tests; on
    # for the research profiles where realistic stake matters.
    round_contracts: bool = False
    taker_fee_rate: float = 0.07
    maker_fee_rate: float = 0.0175
    fee_multiplier: float = 1.0
    limit_price_tick: float = 0.01
    limit_price_edge_lcb_buffer: float = 0.02
    min_conditional_samples: int = 35
    shrinkage_samples: int = 70
    empirical_weight: float = 0.75
    confidence_z: float = 1.96
    market_prior_weight: float = 0.45
    min_model_weight: float = 0.35
    source_spread_market_weight_per_f: float = 0.04
    market_prior_min_reliability: float = 0.20
    market_prior_full_depth: float = 25.0
    market_prior_tight_spread: float = 0.02
    market_prior_wide_spread: float = 0.12
    market_disagreement_lcb_penalty: float = 0.35
    ensemble_weight: float = 0.30
    ensemble_min_members: int = 10
    ensemble_disagreement_lcb_penalty: float = 0.20
    # Flow-dependent sharpening: blend today's GFS ensemble spread into the
    # static residual sigma so the model gets sharper on calm, predictable days
    # and wider on blow-up days. Floored at a fraction of the residual sigma
    # because GFS ensembles are under-dispersive and must not collapse sigma.
    ensemble_sigma_weight: float = 0.30
    ensemble_sigma_floor_frac: float = 0.6
    # Smoothing bandwidth (F) applied to the empirical residual histogram so a
    # ~35-sample window stops emitting spurious 0.0 tail bins. 0 disables it
    # (raw histogram counts).
    empirical_kernel_bandwidth_f: float = 1.0
    # When True, the edge/edge_lcb gate is measured against the pure weather
    # model probability instead of the market-blended posterior, so a liquid
    # market does not erase the model's disagreement (its edge source) before
    # the gate sees it. Sizing still uses the blended, LCB-weighted probability.
    # Off for the trading-intent profiles pending a walk-forward backtest; on
    # for the research profiles that exist to collect samples.
    edge_gate_uses_model_probability: bool = False
    intraday_probability_weight: float = 0.65
    intraday_boundary_watch_f: float = 0.35
    intraday_boundary_weight_boost: float = 0.15
    intraday_min_sigma_f: float = 0.25
    # Cap must stay above the pre-dawn sigma floor (~3F) or the intraday
    # gaussian goes back to crushing high-bracket tails overnight.
    intraday_max_sigma_f: float = 3.25
    # When the forecast sources disagree by more than this many degrees F,
    # the point blend has no business making confident bracket bets: the
    # 2026-06-10 losses all entered with source spread 9.6-11.0F while the
    # blend missed the settled high by ~4F.
    max_source_spread_f: float = 6.0
    cheap_tail_max_ask: float = 0.05
    cheap_tail_min_yes_bid: float = 0.01
    cheap_tail_min_yes_bid_size: float = 25.0
    cheap_tail_min_probability_lcb: float = 0.12
    cheap_tail_min_edge_lcb: float = 0.07
    cheap_tail_max_model_market_gap: float = 0.08
    cheap_tail_min_ensemble_probability: float = 0.08


BALANCED_PROFILE_OVERRIDES = {
    # Paper-trading default. The first live month (Jun 2026) proved that
    # negative lower-bound edge collects only noise: 190 approved trades with
    # edge_lcb < 0 produced a 3/190 win rate, while sub-5c tails won 1.9%
    # against a modeled 8.7%. Balanced now keeps the conservative statistical
    # floor (edge_lcb >= 0) and differs from conservative only in a slightly
    # lower headline-edge bar and a longer forecast-age allowance.
    "min_edge": 0.02,
    "min_edge_lcb": 0.00,
    "max_spread": 0.07,
    "max_model_market_gap": 0.15,
    # Balanced previously inherited conservative's strict cheap-tail floors and
    # a 0.10 posterior minimum, so it almost never cleared a tradeable
    # mid-priced bin (spread aside, this was the dominant volume suppressor on
    # good-data days). These give balanced its own, looser-but-still-guarded
    # floors so it can take mid-ladder trades, while the proven edge_lcb >= 0
    # floor that fenced off the 3/190 negative-LCB failure is kept unchanged.
    # PENDING: validate with a walk-forward, after-fee backtest before treating
    # these as final for real money (see docs/codebase_audit_2026-06-15.md).
    "min_posterior_probability": 0.07,
    "cheap_tail_min_yes_bid_size": 10.0,
    "cheap_tail_min_probability_lcb": 0.09,
    "cheap_tail_min_edge_lcb": 0.03,
    "fractional_kelly": 0.10,
    # Size off a less-pessimistic blend of the point estimate and its lower
    # bound rather than the pure LCB. kelly_lcb_weight=1.0 sized off the LCB
    # alone, which on a typical favorite (point ~0.94 vs LCB ~0.89) cut the Kelly
    # spend ~4x AND double-counted the same uncertainty already enforced by the
    # edge_lcb>=0 approval gate. 0.6 keeps a conservative tilt toward the lower
    # bound while letting size track the actual edge. Held to the balanced
    # (real-trading-intent) profile; conservative base stays at the strict 1.0.
    "kelly_lcb_weight": 0.6,
    # Realistic paper stake (see round_contracts above).
    "round_contracts": True,
    # Sizing retune (2026-06-16, see docs/trading_engine_diagnosis_2026-06-16.md).
    # The diagnosis proved the per-position dollar cap, NOT fractional_kelly, was
    # the binding throttle: Kelly's spend budget on a typical favorite (~$44-67)
    # is ~9x the old $5 cap, so the bot flat-bet ~1% of full Kelly and profit was
    # bounded by stake, not edge (combined realized PnL was ~-$0.17 on $1000).
    # Raising the cap to $20 makes the cap a tail guard rather than the throttle:
    # effective Kelly fraction k ~= $20/$444 ~= 0.045 (under half-Kelly), which
    # MacLean/Thorp/Ziemba (2010) put at ~9% of full-Kelly growth with
    # P(50% drawdown) < 0.1% -- a deliberately safe fraction. Kept to the
    # balanced (positive-ROI) profile only; fast-feedback stays tiny so a bad
    # research idea cannot scale a loss. NOTE: validate the +15.6% with a
    # walk-forward, after-fee backtest before treating this as real-money-ready.
    "max_position_risk_pct": 0.02,
    "max_event_risk_pct": 0.04,
    "max_target_exposure_pct": 0.06,
    "max_forecast_age_hours": 12.0,
    "max_contracts_per_market": 40.0,
    "max_source_spread_f": 7.0,
    # Size against live paper equity (bankroll + realized PnL) so sizing
    # compounds correctly once the bigger caps let PnL accumulate -- Kelly
    # requires sizing off current wealth (Kelly 1956; Thorp 2006). Scoped to the
    # live-running research profiles; the conservative base stays frozen-notional
    # so the strict test baseline remains reproducible. Effect is ~nil today
    # (equity ~= $1000) and grows only as the book does.
    "size_against_live_equity": True,
}


EXPLORATORY_PROFILE_OVERRIDES = {
    # Paper-data collection mode for sparse markets. This should never be used
    # as a live-money profile; it trades a slightly looser statistical bar for
    # much smaller size. Structural gates (relative spread, re-entry cap,
    # target exposure) stay identical to balanced.
    **BALANCED_PROFILE_OVERRIDES,
    # Don't inherit balanced's live-equity sizing: this paper-data profile is
    # deliberately tiny and frozen-notional, so keep the flag explicit here
    # rather than letting it leak in via the spread above.
    "size_against_live_equity": False,
    "min_edge": 0.01,
    "min_edge_lcb": -0.01,
    "max_spread": 0.08,
    "max_model_market_gap": 0.20,
    "min_posterior_probability": 0.08,
    "fractional_kelly": 0.05,
    "max_position_risk_pct": 0.003,
    "max_event_risk_pct": 0.0075,
    "max_target_exposure_pct": 0.015,
    "max_contracts_per_market": 5.0,
    "max_source_spread_f": 8.0,
    "cheap_tail_min_probability_lcb": 0.10,
    "cheap_tail_min_edge_lcb": 0.04,
    "edge_gate_uses_model_probability": True,
}


FAST_FEEDBACK_PROFILE_OVERRIDES = {
    # Paper-only acceleration mode. This is intentionally easier to trigger
    # than exploratory so the project can collect enough entries to learn from,
    # while position size is capped hard enough that bad research ideas stay
    # small in the paper journal.
    **BALANCED_PROFILE_OVERRIDES,
    # Don't inherit balanced's live-equity sizing: fast-feedback stays tiny and
    # frozen-notional so a bad research idea can't compound; keep it explicit.
    "size_against_live_equity": False,
    "min_edge": 0.005,
    # Frequency retune (2026-06-16, see docs/trading_engine_diagnosis_2026-06-16.md).
    # The lower-bound-edge floor was the single most-binding gate: it rejected
    # 19/24 live candidates and was the SOLE blocker on all 4 genuine
    # positive-point-edge candidates (their prob_lcb is haircut ~0.17 for
    # model-vs-market disagreement, dragging edge_lcb negative while point edge
    # stays strongly positive). Loosening -0.03 -> -0.07 recovers exactly 2
    # positive-EV trades (approval 0% -> 8%) that are negative only under the
    # variance buffer, never under EV. Research-profile only; downside is capped
    # at ~$2/trade. The proven edge_lcb >= 0 floor on balanced/conservative that
    # fenced off the documented 3/190 negative-LCB failure is left UNCHANGED.
    "min_edge_lcb": -0.07,
    "max_spread": 0.08,
    "max_spread_fraction_of_cost": 0.50,
    "min_yes_bid": 0.01,
    "min_yes_bid_size": 1.0,
    "max_model_market_gap": 0.25,
    "min_posterior_probability": 0.05,
    "fractional_kelly": 0.02,
    "kelly_lcb_weight": 0.5,
    "max_position_risk_pct": 0.002,
    "max_event_risk_pct": 0.005,
    "max_target_exposure_pct": 0.010,
    "max_contracts_per_market": 3.0,
    # Paper feedback can tolerate moderate source disagreement, but not the
    # 2026-06-10/12 regime where models were separated by double-digit F.
    "max_source_spread_f": 10.0,
    "cheap_tail_min_yes_bid": 0.01,
    "cheap_tail_min_yes_bid_size": 5.0,
    "cheap_tail_min_probability_lcb": 0.06,
    "cheap_tail_min_edge_lcb": 0.02,
    "cheap_tail_max_model_market_gap": 0.12,
    "cheap_tail_min_ensemble_probability": 0.06,
    "edge_gate_uses_model_probability": True,
}


def normalize_risk_profile_name(profile: str | None = None) -> str:
    normalized = (profile or os.getenv("PAPER_RISK_PROFILE", "balanced")).strip().lower()
    normalized = normalized.replace("_", "-")
    if normalized in ("", "conservative"):
        return "conservative"
    if normalized == "balanced":
        return "balanced"
    if normalized == "exploratory":
        return "exploratory"
    if normalized in ("fast", "fast-feedback"):
        return "fast-feedback"
    raise ValueError("risk profile must be conservative, balanced, exploratory, or fast-feedback")


def strategy_config_for_profile(profile: str | None = None) -> StrategyConfig:
    normalized = normalize_risk_profile_name(profile)
    base = StrategyConfig()
    if normalized == "conservative":
        return base
    if normalized == "balanced":
        return StrategyConfig(**{**base.__dict__, **BALANCED_PROFILE_OVERRIDES})
    if normalized == "exploratory":
        return StrategyConfig(**{**base.__dict__, **EXPLORATORY_PROFILE_OVERRIDES})
    if normalized == "fast-feedback":
        return StrategyConfig(**{**base.__dict__, **FAST_FEEDBACK_PROFILE_OVERRIDES})
    raise ValueError("risk profile must be conservative, balanced, exploratory, or fast-feedback")


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)
