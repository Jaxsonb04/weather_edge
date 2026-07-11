# Trade Engine Parameter Audit

Date: 2026-06-12

> Historical research record. Values and single-market assumptions below are
> preserved for provenance; current operations use fifteen city markets, two
> paper profiles, and maker-first portfolio scanning.

This note inventories the important hard-coded WeatherEdge trade-engine
numbers and recommends the next parameter set. It is intentionally conservative:
papers can tell us how to choose parameters, but they cannot make the exact SFO
Kalshi numbers optimal without a point-in-time, after-fee, out-of-sample
backtest.

## Research Basis

- Kelly (1956), ["A New Interpretation of Information Rate"](https://www.princeton.edu/~wbialek/rome/refs/kelly_56.pdf):
  size repeated bets by expected log growth, not by expected dollars.
  WeatherEdge's Kelly sizing is directionally right, but should use
  uncertainty-adjusted probability for real trading.
- Gneiting and Raftery (2007), ["Strictly Proper Scoring Rules, Prediction, and
  Estimation"](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf):
  probability forecasts should be evaluated with proper scoring rules and
  calibration, not just hit rate.
- Bailey, Borwein, Lopez de Prado, and Zhu (2015), ["The Probability of
  Backtest Overfitting"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253):
  parameter searches over many gates need cross-validation and explicit overfit
  control.
- Carr and Lopez de Prado (2014), ["Determining Optimal Trading Rules without
  Backtesting"](https://arxiv.org/abs/1408.1159): repeatedly tuning historical
  simulations invites overfit; use theory and process assumptions before
  sweeping many variants.
- Zambelli (2016), ["Determining Optimal Stop-Loss Thresholds via Bayesian
  Analysis of Drawdown Distributions"](https://arxiv.org/abs/1609.00869):
  stop-loss levels should be learned from drawdown distributions, not set
  arbitrarily.
- Lipton and Lopez de Prado (2020), ["A closed-form solution for optimal
  mean-reverting trading strategies"](https://arxiv.org/abs/2003.10502):
  profit-taking and stop-out levels are coupled to transaction costs and the
  process dynamics.
- [Kalshi API rate-limit docs](https://docs.kalshi.com/getting_started/rate_limits)
  and [fee-rounding docs](https://docs.kalshi.com/getting_started/fee_rounding):
  current scanner/monitor timers are nowhere near exchange rate limits; timer
  choices should be driven by information arrival and API budget, not Kalshi
  throttling.

## Current Evidence

The public Strategy Lab artifact generated at `2026-06-12T07:40:45Z` reports:

- `balanced`: 0 opened trades, 0 resolved trades.
- `fast-feedback`: 9 opened, 5 closed/resolved losers, -$1.54 realized PnL on
  $2.60 resolved capital, ROI `-59.23%`, 4 open positions, $8.71 open risk.
- Signal sample is still small: 5,352 raw rows, 72 deduped settled signals, 24
  settled signals, and only 2 deduped approved settled signals.
- The config already records an important live finding: 190 approved trades with
  negative lower-confidence edge produced a 3/190 win rate.

That does not statistically prove the final optimal gates, but it is enough to
reject the current `fast-feedback` setting of `min_edge_lcb = -0.18` as a
trading-intent default. It is a paper-data collection setting that admits
strongly negative lower-bound expected value.

## Recommended Constants

### Balanced

Balanced should be the only profile treated as trading-intent.

| Parameter | Current | Recommended |
| --- | ---: | ---: |
| `min_edge` | `0.02` | `0.02` |
| `min_edge_lcb` | `0.00` | `0.00` |
| `max_spread` | `0.07` | `0.07` |
| `max_spread_fraction_of_cost` | `0.35` | `0.35` |
| `max_model_market_gap` | `0.15` | `0.15` |
| `min_posterior_probability` | `0.10` | `0.10` |
| `fractional_kelly` | `0.12` | `0.10` |
| `kelly_lcb_weight` | `1.0` | `1.0` |
| `max_position_risk_pct` | `0.01` | `0.005` |
| `max_event_risk_pct` | `0.03` | `0.015` |
| `max_target_exposure_pct` | `0.05` | `0.025` |
| `max_contracts_per_market` | `25` | `10` |
| `max_forecast_age_hours` | `36` | `12` |
| `max_source_spread_f` | `7` | `7` |

Reasoning: keep the proven LCB floor, reduce concentration risk, and stop
letting stale forecasts survive a failed refresh cycle for more than half a day.

### Fast Feedback

Fast feedback can be more active, but it should not knowingly buy deeply
negative-LCB trades. More trades are useful only if they are useful samples.

| Parameter | Current | Recommended |
| --- | ---: | ---: |
| `min_edge` | `0.00` | `0.005` |
| `min_edge_lcb` | `-0.18` | `-0.03` |
| `max_spread` | `0.12` | `0.08` |
| `max_spread_fraction_of_cost` | `0.75` | `0.50` |
| `max_model_market_gap` | `0.40` | `0.25` |
| `min_posterior_probability` | `0.03` | `0.05` |
| `fractional_kelly` | `0.03` | `0.02` |
| `kelly_lcb_weight` | `0.0` | `0.50` |
| `max_position_risk_pct` | `0.003` | `0.002` |
| `max_event_risk_pct` | `0.010` | `0.005` |
| `max_target_exposure_pct` | `0.015` | `0.010` |
| `max_contracts_per_market` | `5` | `3` |
| `max_forecast_age_hours` | `36` | `12` |
| `max_source_spread_f` | `18` | `10` |

This keeps fast-feedback materially looser than balanced through spread,
model-gap, posterior, and LCB tolerances, while preventing the worst known
failure mode.

### Cheap Tail Gates

Cheap YES tails have been the live failure mode. They need exceptional support.

| Parameter | Current fast-feedback | Recommended fast-feedback |
| --- | ---: | ---: |
| `cheap_tail_min_yes_bid_size` | `1` | `5` |
| `cheap_tail_min_probability_lcb` | `0.02` | `0.06` |
| `cheap_tail_min_edge_lcb` | `-0.03` | `0.02` |
| `cheap_tail_max_model_market_gap` | `0.25` | `0.12` |
| `cheap_tail_min_ensemble_probability` | `0.03` | `0.06` |

### Exits

Current exits are symmetric: take-profit `35%`, stop-loss `35%`, model veto hard
floor `60%`. The next version should make exits side-aware:

| Rule | Recommended |
| --- | ---: |
| YES take profit | `50%` |
| YES stop loss | `25%` |
| YES model veto | disabled |
| NO take profit | `35%` |
| NO stop loss | `35%` |
| NO model-veto hard floor | `60%` |
| NO model-veto buffer | `0.08` probability / dollars |

If side-aware exits are not implemented yet, use global take-profit `40%`,
stop-loss `35%`, and model-veto hard floor `60%`.

### Timers

Kalshi's current documented Basic tier is 200 read tokens/s and 100 write
tokens/s, and most requests cost 10 tokens. WeatherEdge's timers are far below
that. Google/weather data budget is the tighter constraint.

| Timer | Current | Recommended |
| --- | --- | --- |
| Forecaster refresh | active 30m, off 60m | keep |
| Paper scan | 15m | keep until gates are fixed; 10m is acceptable after |
| Paper monitor | 5m | 2m while positions are open |
| Paper settle | 30m | keep |
| Strategy Lab refresh | 5m | keep |
| Dataset backfill | nightly 02:25 | keep |

Increasing scan frequency will not solve under-trading if gates reject the
market. The live Jun 12 artifact had a 13.8F source spread, so balanced was
right to block trading even though Kalshi was liquid.

### Calibration And Research Gates

| Parameter | Current | Recommended |
| --- | ---: | ---: |
| `min_conditional_samples` | `35` | `35` |
| `shrinkage_samples` | `70` | `70` |
| `empirical_weight` | `0.75` | `0.65` after calibration test |
| `confidence_z` | `1.96` | `1.96` balanced, `1.64` only for research displays |
| `ensemble_weight` | `0.30` | keep |
| `ensemble_min_members` | `10` | keep |
| `market_prior_weight` | `0.45` | keep |
| `min_model_weight` | `0.35` | keep |
| `calibration_min_train` | `180` | keep |
| dataset `min_matched_rows` | `30` | `60` |
| dataset `min_after_cost_trades` | `30` | `50` |

Do not loosen execution gates based on dataset candidates until the after-cost
market gate passes.

## New Guardrail Needed

Add a fast-feedback kill switch:

- Pause fast-feedback paper entries if resolved trades are at least `5` and ROI
  is below `-25%`.
- Pause fast-feedback paper entries for the day after `-$5` realized PnL on a
  `$1000` paper bankroll.
- Continue recording rejected and near-miss decision snapshots while paused.

This preserves learning while preventing the dashboard from repeatedly spending
paper risk into a known-bad regime.

## Implementation Order

1. Tighten `FAST_FEEDBACK_PROFILE_OVERRIDES`.
2. Reduce balanced concentration and stale-forecast limits.
3. Add side-aware monitor exits and a configurable veto buffer.
4. Add the fast-feedback kill switch.
5. Then run a walk-forward, after-fee parameter sweep with recorded trials and
   out-of-sample reporting before calling any value "optimal."
