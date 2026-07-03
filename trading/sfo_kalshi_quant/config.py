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
    # Minimum modelled uncertainty applied when SIZING (not when gating). A
    # day-ahead 2F bin can never be known with zero error -- the live
    # calibration gap is ~0.28 -- yet intraday conditioning or a saturated
    # normal-CDF can drive the side probability and its lower bound to a literal
    # 1.0, which erases the uncertainty haircut and max-sizes Kelly off false
    # certainty (the over-sizing behind the 22/16-contract NO favorites on
    # 2026-06-18). Clamping the sizing probability to [u, 1-u] leaves ordinary
    # favorites (p ~0.85-0.93) untouched while capping degenerate certainty.
    # 0.0 on the frozen baseline for reproducible tests; enabled per profile.
    min_probability_uncertainty: float = 0.0
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
    # --- Kalshi market-consensus anchor (the "huge consideration" addon) -----
    # The bid/ask ladder encodes the crowd's forecast of the SFO high (extracted
    # in consensus.py: implied high, distribution, percentiles). That consensus
    # is ALWAYS surfaced in the report/CLI/dashboard. These flags gate only
    # whether it gets a HEAVIER voice in the traded posterior and a guard against
    # over-betting a confident, liquid market. Default OFF on live pending a
    # walk-forward backtest; ON for research to collect validation samples.
    market_consensus_anchor_enabled: bool = False
    # Replaces market_prior_weight (0.45) as the base market weight in
    # _model_weight when anchoring is on. Reliability scaling and the model-
    # weight floor still apply, so a thin/wide market still cannot dominate.
    market_consensus_anchor_weight: float = 0.60
    # Model-weight floor when anchoring (replaces min_model_weight=0.35). Keeps
    # the edge alive: the weather model is never silenced below this share, so
    # its residual disagreement with the market remains the trade's edge source.
    market_consensus_anchor_min_model_weight: float = 0.30
    # Size guard: when our point forecast disagrees with the market-implied
    # consensus high by >= guard_gap_f AND the market is confident (implied stdev
    # <= guard_max_stdev_f), liquid (>= guard_min_bins two-sided bins), and
    # well-formed (|overround| <= guard_max_overround), haircut the position size
    # by guard_size_haircut. This is the "don't bet hard against a confident,
    # liquid market" guard. It NEVER creates or blocks a trade; it only shrinks
    # size on a bet that already cleared every gate. Default OFF on live.
    market_consensus_guard_enabled: bool = False
    market_consensus_guard_gap_f: float = 4.0
    market_consensus_guard_max_stdev_f: float = 3.0
    market_consensus_guard_min_bins: int = 4
    market_consensus_guard_max_overround: float = 0.25
    market_consensus_guard_size_haircut: float = 0.5
    ensemble_weight: float = 0.30
    ensemble_min_members: int = 10
    ensemble_disagreement_lcb_penalty: float = 0.20
    # Flow-dependent sharpening: blend today's GFS ensemble spread into the
    # static residual sigma so the model gets sharper on calm, predictable days
    # and wider on blow-up days. Floored at a fraction of the residual sigma
    # because GFS ensembles are under-dispersive and must not collapse sigma.
    ensemble_sigma_weight: float = 0.30
    ensemble_sigma_floor_frac: float = 0.6
    # When True AND a trained EMOS (mu, sigma) is supplied to the calibrator, the
    # weather distribution is built directly from the EMOS Gaussian (Phase 1
    # proved it beats climatology and the heuristic blend on CRPS) instead of the
    # residual-calibrated point + bolted-on sigma. Off for live pending a
    # walk-forward rescore; on for the research collector. Identity when disabled
    # OR when no EMOS forecast is available for the target day, in which case the
    # CLI/report path degrades to residual calibration and Strategy Lab health
    # should show the missing live EMOS target.
    emos_distribution_enabled: bool = False
    # Phase 2b -- posterior-mean Kelly. When True and a PosteriorKellyModel is
    # injected, the base fractional_kelly is multiplied by a per-cohort trust
    # factor learned from the settled journal: size grows only as a real edge is
    # demonstrated and shrinks on cohorts the engine keeps losing (Baker & McHale
    # 2013; Chu, Wu & Swartz 2018). Off (identity) by default -- the frozen
    # baseline; enabled per profile after a walk-forward rescore. The prior is
    # centered on breakeven so a short record shrinks toward the floor; the floor
    # keeps a data-collecting profile filling the journal (set 0.0 to stand a
    # real-money profile down on unproven cohorts).
    posterior_mean_kelly_enabled: bool = False
    posterior_mean_kelly_prior_strength: float = 20.0
    posterior_mean_kelly_floor: float = 0.2
    posterior_mean_kelly_min_cohort_n: int = 8
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
    # --- Comfortable far-tail NO entry (the "edge" idea) ---------------------
    # When True, NO bets are gated and sized by how far the market bin sits from
    # the point forecast. Bins comfortably out in the tail -- where a forecast
    # miss of a few F cannot reach -- are the high-confidence NO favorites; bins
    # near the forecast are coin-flips and the documented live loss source (the
    # 06-14/06-17 B72.5/B74.5/B76.5 NO bets all settled in-bin against a ~2.46F
    # mean forecast error). With this on, near-forecast NO bets are blocked and
    # far-tail NO bets are sized up -- but every existing gate AND the positive
    # after-fee edge_lcb floor still bind, so it never creates a negative-EV
    # bet. Off on the strict baseline and the research collector (which collects
    # the full opportunity set, center bins included); on for the live profile.
    comfort_edge_enabled: bool = False
    # Comfort distances are uncertainty-scaled: a multiple of the day's forecast
    # sigma, floored so a calm-day band cannot collapse below the irreducible
    # single-model error. The floor is set to ~p75 of the REALIZED |forecast
    # error| (not the mean 2.46F and not source agreement, which understate a
    # calm day's true miss): realized error is mean 3.2F, p90 ~6.4F, so a 3.0F
    # floor puts the block band at ~3.75F (covers ~p75 of misses) and full size
    # at ~7.5F (~p93) -- i.e. a bin is only "comfortably far" once a typical miss
    # cannot reach it. Source spread still widens the band above the floor on
    # disagreement days; the floor applies when no per-day sigma is threaded.
    comfort_edge_sigma_floor_f: float = 3.0
    # NO bets whose bin sits within block_mult * sigma of the forecast are
    # rejected as coin-flips (the loss source). 1.25 * 3.0 = 3.75F.
    comfort_edge_block_sigma_mult: float = 1.25
    # NO bets at/beyond full_mult * sigma get the full size boost; the boost
    # tapers linearly from the block band up to here. 2.5 * 3.0 = 7.5F (~p93 of
    # realized forecast error, so full confidence means a typical miss can't reach it).
    comfort_edge_full_sigma_mult: float = 2.5
    # Max Kelly size multiplier applied to a NO bet at/beyond the full distance
    # (1.0 = no boost). The boost still passes every gate and the positive
    # after-fee edge_lcb floor, so it only leans harder into far tails that
    # already clear the gates -- it never manufactures a bet that was not there.
    comfort_edge_max_size_boost: float = 2.0
    # Research shadow sampling: the research profile still records every
    # point-positive exploration candidate, but only a deterministic sample becomes
    # a real paper position. This keeps the learning set wide while preventing the
    # paper PnL stream from being dominated by intentionally-uncertain trades.
    research_shadow_sample_probability: float = 0.25
    research_shadow_max_contracts: float = 1.0
    research_shadow_daily_loss_pct: float = 0.0025


LIVE_PROFILE_OVERRIDES = {
    # The real-money-INTENT profile (paper-only until the readiness gate in
    # backtest_rescore.py passes). The first live month (Jun 2026) proved that
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
    # WARM UNBLOCKED (2026-07-02, Phase 2a). The "Brier ~0.96" that justified the
    # warm block was the BLEND's number; the served emos_wmean distribution is
    # calibrated on warm (walk-forward shared-sigma Brier 0.895, on par with cold
    # 0.903 / normal 0.862 -- see docs/PHASE0-findings.md). Warm is the dominant
    # summer regime, so blocking it left the book idle exactly when it should
    # trade. HOT stays blocked here: it is genuinely harder (CRPS ~2.2 vs ~1.2)
    # and rare, so it is left to the research collector and to size-gating
    # (posterior-mean Kelly + its wider sigma) rather than an outright block.
    "blocked_forecast_cohorts": (HOT_COHORT,),
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
    # Volatility retune (2026-06-18): nudged 0.25 -> 0.30 (between quarter- and
    # third-Kelly, still well below half-Kelly) so thin-edge trades whose Kelly
    # budget sits below the per-position cap also deploy more -- more swing on the
    # days the position cap is NOT the binding lever. PENDING walk-forward validation.
    "fractional_kelly": 0.30,
    # Posterior-mean Kelly ON as a light safety valve (Phase 2b) now that warm is
    # unblocked: it does NOT reduce trade frequency, only per-trade size, scaling
    # up automatically as a cohort proves out. A GENEROUS 0.4 floor keeps real
    # size on unproven cohorts (0.4 x 0.30 fractional Kelly) so the book stays
    # active rather than idle -- raise the floor toward 1.0 for more aggression,
    # lower it to stand down harder on unproven regimes.
    "posterior_mean_kelly_enabled": True,
    "posterior_mean_kelly_floor": 0.4,
    # Size off a less-pessimistic blend of the point estimate and its lower
    # bound rather than the pure LCB. kelly_lcb_weight=1.0 sized off the LCB
    # alone, which on a typical favorite (point ~0.94 vs LCB ~0.89) cut the Kelly
    # spend ~4x AND double-counted the same uncertainty already enforced by the
    # edge_lcb>=0 approval gate. 0.6 keeps a conservative tilt toward the lower
    # bound while letting size track the actual edge. Held to the balanced
    # (real-trading-intent) profile; conservative base stays at the strict 1.0.
    "kelly_lcb_weight": 0.6,
    # Cap sizing certainty: a degenerate p/LCB of 1.0 (intraday conditioning or
    # CDF saturation) must not max-size Kelly on a day-ahead 2F bin. See the
    # field comment on StrategyConfig.min_probability_uncertainty.
    "min_probability_uncertainty": 0.04,
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
    #
    # VOLATILITY RETUNE (2026-06-18): the old 5%/8%/12% caps left the equity inert
    # ($1-2 swings) because the $50 per-position cap bound below quarter-Kelly's
    # $87-128 want on a real edge. Raised to 8%/12%/18% so Kelly's chosen size
    # actually flows and the book visibly moves -- worst-case single-day loss is
    # bounded to ~18% of equity (~$180 on $1000), which is the owner's stated
    # paper appetite. The positive after-fee edge_lcb>=0 floor is UNCHANGED, so a
    # bigger size is still a positive-EV bet, never a manufactured negative-EV one.
    # PENDING walk-forward validation before any real-money use.
    "max_position_risk_pct": 0.08,
    "max_event_risk_pct": 0.12,
    "max_target_exposure_pct": 0.18,
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
    # Comfortable far-tail NO entry (see StrategyConfig.comfort_edge_*). Live
    # blocks near-forecast coin-flip NO bets (the documented loss source) and
    # sizes up genuine far tails, keeping the positive after-fee edge_lcb floor.
    # The research collector leaves this OFF so it still records the center bins
    # the readiness rescore needs to prove the rule actually helps.
    "comfort_edge_enabled": True,
}


# The single data-collection profile (merged from the former `exploratory` and
# `fast-feedback` books). It takes the loosest gates so it approves the widest
# opportunity set, at the smallest size so a bad research idea stays tiny in the
# paper journal -- "collect as much data as possible, risk almost nothing." It
# is paper-only and is never a real-money candidate; only `live` is judged by
# the real-money readiness gate. Legacy `exploratory`/`fast-feedback`/`fast`
# names normalize to this profile so historical paper books roll into it.
RESEARCH_PROFILE_OVERRIDES = {
    # Starts from the live gates, then takes the LOOSEST bar of the two former
    # collectors (so it approves the widest opportunity set) at the SMALLEST
    # size of the two (so a bad research idea stays tiny). Reconciled from the
    # old exploratory + fast-feedback dicts: loosest of {min_edge 0.008/0.005,
    # min_edge_lcb -0.05/-0.07, posterior 0.06/0.05, gap 0.20/0.25, source
    # spread 8/10}, smallest of {kelly 0.15/0.12, pos_risk 0.02/0.015, event
    # 0.04/0.03, target 0.06/0.05, contracts 30/25}. Effectively the old
    # fast-feedback envelope, which was already the loosest + smallest.
    **LIVE_PROFILE_OVERRIDES,
    # Size off live equity so this book compounds and fluctuates; the small
    # per-day risk budget below keeps a bad research idea bounded.
    "size_against_live_equity": True,
    # The collector records raw/marginal YES to learn whether YES can work; the
    # strict YES gates belong on live (the exploiter), not here.
    "yes_estimation_shrink": False,
    # The collector trades the warm/hot regime to collect the calibration data
    # recalibration needs; the regime block is live-only.
    "blocked_forecast_cohorts": (),
    # The collector records the FULL opportunity set (center bins included) so
    # the readiness rescore can prove the comfort-edge rule helps before live
    # adopts it; comfort gating/sizing is therefore off here.
    "comfort_edge_enabled": False,
    # The loosest collector must record the widest forecast-age window: it would
    # otherwise silently inherit live's tightened 12h freshness gate via the
    # **LIVE_PROFILE_OVERRIDES spread, which is STRICTER than the base 30h and
    # discards exactly the 12-30h-old snapshots the collector exists to gather.
    "max_forecast_age_hours": 30.0,
    # Loosest point gates of the two former collectors.
    "min_edge": 0.005,
    # The lower-bound-edge floor is the EV guardrail, so it is loosened far less
    # than the point gates; -0.07 was the loosest the two collectors used. The
    # proven edge_lcb >= 0 floor on the LIVE profile is left UNCHANGED.
    "min_edge_lcb": -0.07,
    "max_spread": 0.10,
    "max_spread_fraction_of_cost": 0.50,
    "max_model_market_gap": 0.25,
    "min_posterior_probability": 0.05,
    "min_yes_bid": 0.01,
    "min_yes_bid_size": 1.0,
    # Smallest size of the two former collectors -- tiny on purpose.
    "fractional_kelly": 0.12,
    "kelly_lcb_weight": 0.5,
    "max_position_risk_pct": 0.015,
    "max_event_risk_pct": 0.03,
    "max_target_exposure_pct": 0.05,
    "max_contracts_per_market": 25.0,
    # Allow re-entry after a close (lifetime cap 3) instead of one-and-done per
    # market/side. has_active_paper_entry still blocks concurrent double-holding,
    # so this only lets a closed position be re-entered when the edge returns --
    # more turnover, not more simultaneous risk. Collector-only; live stays 1.
    "max_entries_per_market_side": 3,
    # Tolerates moderate source disagreement to collect more, but not the
    # 2026-06-10/12 regime where models were separated by double-digit F.
    "max_source_spread_f": 10.0,
    "cheap_tail_min_yes_bid": 0.01,
    "cheap_tail_min_yes_bid_size": 5.0,
    "cheap_tail_min_probability_lcb": 0.06,
    "cheap_tail_min_edge_lcb": 0.02,
    "cheap_tail_max_model_market_gap": 0.12,
    "cheap_tail_min_ensemble_probability": 0.06,
    "edge_gate_uses_model_probability": True,
    # Research-first: build bucket probabilities from the trained EMOS Gaussian
    # (Phase 1: rolling-origin EMOS beats climatology and the blend on CRPS, with
    # a calibrated sigma) so the rescore can prove it improves trading calibration
    # before `live` adopts it. Identity when no EMOS forecast exists for the day.
    "emos_distribution_enabled": True,
    # The collector runs the market-consensus anchor + guard LIVE so the
    # readiness rescore can prove they help before `live` adopts them. The
    # heavier market weight is safe here because the edge gate already measures
    # the POINT edge against the pure model probability (above), so a strong
    # anchor leans sizing/LCB toward the crowd without erasing the model's
    # measured disagreement -- exactly the validation signal we want to collect.
    "market_consensus_anchor_enabled": True,
    "market_consensus_guard_enabled": True,
}


# Two profiles only: `live` (the real-money-INTENT exploiter, paper-only until
# the readiness gate passes) and `research` (the data collector). Legacy names
# are accepted as aliases and fold into the survivor so historical paper books
# and stored `risk_profile` strings keep rolling up correctly:
#   balanced, conservative   -> live
#   exploratory, fast-feedback, fast, collector, explore -> research
# This is also the read-side half of the rename: any legacy string read out of
# the DB normalizes to the new profile; db.init() additionally rewrites the
# stored strings once so raw SQL filters stay correct.
_LIVE_ALIASES = {"", "live", "balanced", "conservative", "real"}
_RESEARCH_ALIASES = {
    "research",
    "exploratory",
    "fast-feedback",
    "fast",
    "collector",
    "explore",
}


def normalize_risk_profile_name(profile: str | None = None) -> str:
    normalized = (profile or os.getenv("PAPER_RISK_PROFILE", "live")).strip().lower()
    normalized = normalized.replace("_", "-")
    if normalized in _LIVE_ALIASES:
        return "live"
    if normalized in _RESEARCH_ALIASES:
        return "research"
    raise ValueError("risk profile must be 'live' or 'research'")


def strategy_config_for_profile(profile: str | None = None) -> StrategyConfig:
    normalized = normalize_risk_profile_name(profile)
    base = StrategyConfig()
    if normalized == "live":
        return StrategyConfig(**{**base.__dict__, **LIVE_PROFILE_OVERRIDES})
    if normalized == "research":
        return StrategyConfig(**{**base.__dict__, **RESEARCH_PROFILE_OVERRIDES})
    raise ValueError("risk profile must be 'live' or 'research'")


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)
