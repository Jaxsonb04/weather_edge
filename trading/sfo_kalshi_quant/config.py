from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SFO_TZ = ZoneInfo("America/Los_Angeles")
SERIES_TICKER = "KXHIGHTSFO"

COLD_COHORT = "cold_below_60f"
NORMAL_COHORT = "normal_60_69f"
WARM_COHORT = "warm_70_79f"
HOT_COHORT = "hot_80f_plus"


def temperature_cohort(high_f: float) -> str:
    """Temperature regime a forecast/settlement high falls in.

    Boundaries match the calibration cohorts in backtest.py so a per-cohort
    Brier score maps 1:1 to the regime gate. The forecaster is anti-calibrated on
    warm/hot SFO days (cohort Brier ~0.96, worse than a coin flip), so balanced
    blocks those cohorts until recalibration earns them back.
    """

    if high_f < 60.0:
        return COLD_COHORT
    if high_f < 70.0:
        return NORMAL_COHORT
    if high_f < 80.0:
        return WARM_COHORT
    return HOT_COHORT


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
    # YES-side case-by-case sizing. YES longshots are fee-dominated (a 0.09 ask
    # pays ~25% round-trip in fees) and were the live loss source (0/3). When
    # enabled, YES is sized off the conservative lower bound, shrunk for
    # estimation error (Baker-McHale: any probability uncertainty puts the
    # growth-optimal bet strictly below naive Kelly), scaled down by payout, and
    # hard-capped; plus a positive-lower-bound-edge gate and an EV cushion on
    # sub-0.15 YES. Off on the conservative baseline.
    yes_estimation_shrink: bool = False
    yes_max_position_risk_pct: float = 0.005
    # Forecast temperature cohorts to block entirely (the forecaster is
    # anti-calibrated on them). Empty = no regime gate. Tuple of cohort names
    # from temperature_cohort(). The explorer profiles keep these empty so they
    # collect the cohort data recalibration needs.
    blocked_forecast_cohorts: tuple[str, ...] = ()


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
    # Raise the cheap-tail scrutiny ceiling so the 0.08-0.12 YES longshots that
    # lost live actually get tail-grade gating (the old 0.05 ceiling let them
    # through ungated), and size YES case-by-case off the lower bound.
    "cheap_tail_max_ask": 0.15,
    "yes_estimation_shrink": True,
    # Block the warm/hot regime where the forecaster is anti-calibrated (cohort
    # Brier ~0.96). Self-clears by editing this list once recalibration earns the
    # cohort back. Scoped to balanced; the explorer profiles still trade them to
    # collect the data that recalibration needs.
    "blocked_forecast_cohorts": (WARM_COHORT, HOT_COHORT),
    "cheap_tail_min_yes_bid_size": 10.0,
    "cheap_tail_min_probability_lcb": 0.09,
    "cheap_tail_min_edge_lcb": 0.03,
    # Quarter-Kelly (2026-06-17). Kelly is the growth-optimal bet finder: the
    # per-trade size is fractional_kelly x full_Kelly(edge, cost) x live equity,
    # so it scales with BOTH the edge and the current bankroll -- never a hardcoded
    # dollar. 0.10 was so conservative that, combined with the old $20 per-position
    # cap, the book deployed pocket change on a $1000 bankroll. Quarter-Kelly
    # (MacLean/Thorp/Ziemba 2010: ~half-Kelly's growth at far lower drawdown) lets
    # size actually track edge. Conservative base stays at the strict 0.15/1.0.
    "fractional_kelly": 0.25,
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
    # Meaningful-stake retune (2026-06-17). The point: on a $1000 paper book,
    # deploying cents/trade makes the equity inert -- it cannot fluctuate or show
    # whether the edge is real. These caps frame a per-day RISK BUDGET (~12% of
    # live equity) that Kelly fills opportunistically; on a $1000 equity that is
    # up to ~$50/position and ~$120/day, fluctuating with edge and compounding off
    # realized PnL. It is NOT a forced daily spend: on a day with no qualifying
    # edge (e.g. the warm/hot regime balanced blocks) it correctly deploys little,
    # which is the EV-right way to protect the bankroll. max_contracts is lifted
    # to 100 so the DOLLAR caps bind, not an arbitrary contract count.
    # Paper-realism only; the real-money gate (walk-forward, after-fee, per-cohort
    # validation over >=30 independent days) is UNCHANGED. Conservative base stays
    # frozen-notional and strict as the reproducible control.
    "max_position_risk_pct": 0.05,
    "max_event_risk_pct": 0.08,
    "max_target_exposure_pct": 0.12,
    "max_forecast_age_hours": 12.0,
    "max_contracts_per_market": 100.0,
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
    # as a live-money profile; it trades a looser statistical bar for much
    # smaller size. Tuned (2026-06-17) to be a genuine SECOND high-volume
    # collector alongside fast-feedback: same tiny size, but its own paper book
    # (orders are risk_profile-scoped, so it holds positions independently),
    # which roughly doubles paper-trade throughput on the same opportunity set.
    **BALANCED_PROFILE_OVERRIDES,
    # Size off live equity so this book compounds and fluctuates like the others;
    # its per-day risk budget below keeps it well under balanced.
    "size_against_live_equity": True,
    # The explorer collects raw/marginal YES to learn whether YES can work; the
    # strict YES gates belong on balanced (the exploiter), not here.
    "yes_estimation_shrink": False,
    # The explorer trades the warm/hot regime to collect the very calibration
    # data recalibration needs; the regime block is balanced-only.
    "blocked_forecast_cohorts": (),
    # Frequency retune (2026-06-17): loosen the POINT-edge gates so exploratory
    # approves more distinct market/sides than before (0.01 -> 0.008 min_edge,
    # 0.08 -> 0.06 posterior), while staying a notch stricter than fast-feedback
    # (which remains the most-active profile). The lower-bound-edge floor stays
    # bounded (-0.05, tighter than fast-feedback's -0.07) -- the LCB floor is the
    # EV guardrail, so it is loosened far less than the point gates.
    "min_edge": 0.008,
    "min_edge_lcb": -0.05,
    "max_spread": 0.10,
    "max_model_market_gap": 0.20,
    "min_posterior_probability": 0.06,
    # Meaningful-stake retune (2026-06-17): a real (~quarter-Kelly) research book,
    # ~$20/position and ~6%/day on a $1000 equity -- no longer pocket change, but
    # held below balanced so an unproven explorer idea cannot scale a large loss.
    "fractional_kelly": 0.15,
    "max_position_risk_pct": 0.02,
    "max_event_risk_pct": 0.04,
    "max_target_exposure_pct": 0.06,
    "max_contracts_per_market": 30.0,
    # Allow re-entry after a close (lifetime cap 3) instead of one-and-done per
    # market/side. has_active_paper_entry still blocks concurrent double-holding,
    # so this only lets a closed position be re-entered when the edge returns --
    # more turnover, not more simultaneous risk. Explorer-only; balanced stays 1.
    "max_entries_per_market_side": 3,
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
    # Size off live equity so this book compounds and fluctuates; the small
    # per-day risk budget below keeps a bad research idea bounded.
    "size_against_live_equity": True,
    # Explorer collects raw/marginal YES; the strict YES gates are balanced-only.
    "yes_estimation_shrink": False,
    # Explorer trades the warm/hot regime to collect calibration data.
    "blocked_forecast_cohorts": (),
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
    # Frequency retune (2026-06-17): widen the spread ceiling a touch so a few
    # more wider-book markets clear at tiny size, and allow re-entry after a
    # close (lifetime cap 3, guarded by has_active_paper_entry against concurrent
    # holding) so a closed position can be re-bought when the edge returns. The
    # proven edge_lcb >= 0 floor on balanced/conservative is left UNCHANGED.
    "max_spread": 0.10,
    "max_spread_fraction_of_cost": 0.50,
    "max_entries_per_market_side": 3,
    "min_yes_bid": 0.01,
    "min_yes_bid_size": 1.0,
    "max_model_market_gap": 0.25,
    "min_posterior_probability": 0.05,
    # Meaningful-stake retune (2026-06-17): the most-active research book, lifted
    # from ~$2/position pocket change to ~$15/position and ~5%/day on a $1000
    # equity. Stays the smallest live profile (< exploratory < balanced) so the
    # loosest gates never scale a large loss; downside per trade is now ~$15, not
    # ~$2, which is the point -- the book has to move to teach us anything.
    "fractional_kelly": 0.12,
    "kelly_lcb_weight": 0.5,
    "max_position_risk_pct": 0.015,
    "max_event_risk_pct": 0.03,
    "max_target_exposure_pct": 0.05,
    "max_contracts_per_market": 25.0,
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
