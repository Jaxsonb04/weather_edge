# Strategy Notes

> Research record dated 2026-06-12. This documents the SFO-era strategy basis;
> current operations cover fifteen cities with maker-first `live` and `research`
> paper profiles. See `trading/README.md` for active commands.

This project treats the SFO Kalshi market as a probabilistic forecasting and
execution problem.

The final trading probability is now a posterior, not a raw weather-model
number:

```text
P_trade = model_weight * P_weather + market_weight * P_market
```

`P_weather` comes from the calibrated SFO forecaster residual distribution plus
a station-aligned Open-Meteo GFS ensemble shape. The ensemble is not allowed to
move the forecast center by itself; its raw grid members are shifted to the SFO
station-centered forecast and then used for spread and bucket shape. For today's
market it is conditioned on the observed high so far, so a bucket below the
current high is assigned zero weather probability. `P_market` is the normalized
probability implied by Kalshi's live bid/ask ladder, downweighted when the book
is wide, thin, or internally inconsistent.

The selected-side edge is:

```text
edge = P_trade(side wins) - side_ask - fee
```

For BUY_YES, `P_trade(side wins)` is the bucket probability. For BUY_NO, it is
the complement probability, `1 - P_trade(YES)`. The trade is allowed only when
the lower confidence bound of the selected-side edge is still non-negative.
Position size uses fractional Kelly on the amount spent, with a hard per-market
risk cap.

Current gates:

- no live orders
- active Kalshi market only
- ask must be tradeable
- selected-side bid and selected-side bid size must show exit support
- spread must be below the configured maximum
- model/market probability gap must be below the configured maximum
- final posterior probability must clear the configured minimum
- raw edge must clear `min_edge`
- confidence-adjusted edge must clear `min_edge_lcb`
- 1c/2c asks require exceptional bid support, bid depth, lower-bound edge,
  model/market agreement, and ensemble support when available
- size is capped by fractional Kelly, bankroll risk, market depth, and max contracts

## Two profiles: `live` and `research`

There are exactly two risk profiles, and the CLI defaults to `live`.

- `--risk-profile live` is the real-money-INTENT exploiter. It is the stricter,
  real-trading-candidate book — but it stays PAPER-ONLY until the readiness gate
  below passes. It keeps the proven `edge_lcb >= 0` floor, blocks the warm/hot
  cohorts the forecaster is anti-calibrated on, and runs the comfortable
  far-tail NO rule (next section).
- `--risk-profile research` is the single data COLLECTOR (the former
  `exploratory` and `fast-feedback` books, merged). It takes the loosest gates
  so it approves the widest opportunity set, at the smallest size so a bad
  research idea stays tiny. It deliberately records the FULL opportunity set,
  center bins included (comfort-edge OFF), so the readiness rescore can judge
  the live config against everything that was available. It is never promoted to
  real money.

The strict `StrategyConfig()` baseline still exists internally as the
reproducible test control, but it is no longer a selectable profile. Legacy
names are accepted as aliases: `balanced`/`conservative` map to `live`;
`exploratory`/`fast-feedback`/`fast` map to `research` (and stored paper books
under the old names are migrated on read so history rolls up correctly).

Both profiles still reject no-bid tails, but they are less strict on small
1-cent or 2-cent markets when bid support, lower-bound edge, model/market
agreement, and ensemble evidence are all present.

## Comfortable far-tail NO entry (the "edge" rule)

When the point forecast sits comfortably between a market's tail bins — predict
75F and the day lists a "65 or below" or "85 or above" bin — those far edges are
the high-confidence NO favorites: a forecast miss of a couple degrees cannot
reach them. Bins sitting *near* the forecast are the opposite: a ~2.5F mean
forecast error makes a NO bet on the in-forecast bin a coin flip, and those
near-forecast NO bets were the dominant live loss source.

On the `live` profile, `analyze` therefore shapes NO bets by their distance from
the point forecast:

- NO bins within the **block band** of the forecast are rejected as coin flips.
- NO bins at or beyond the **full band** get the full size boost; the boost
  tapers in between.

The band is uncertainty-scaled: it is a multiple of the day's forecast
uncertainty (the source-spread proxy), floored so a calm day blocks within ~3F
and reaches full size ~6F out, and widens when the forecast sources disagree.
The size boost only scales a bet that already cleared every gate and the
positive after-fee `edge_lcb` floor — it never creates or enlarges a negative-EV
bet, so "invest comfortably" never means "buy an over-priced favorite." The
`research` collector leaves this off so it still records the center bins the
readiness rescore needs.

## Knowing when `live` is ready for real money

The Strategy Lab publishes a **real-money readiness** gauge: a single percentage
plus a per-check breakdown for the `live` profile only. It collapses the
walk-forward, after-fee config rescore and the walk-forward per-cohort
calibration into the project's codified go-live bar — positive after-fee
day-clustered ROI lower bound and log-growth per independent day, per side and
per traded cohort, over >=30 independent settlement days, with traded-cohort
Brier < 0.25 and a tight calibration gap. Every check fails closed, so the
verdict only flips to READY when all of them pass simultaneously; until then the
gauge shows how far along the live book is. The `research` collector is never
judged by this gate.

A cheap ask is not enough. If there is no bid, no depth, no lower-bound edge, or
the model is wildly disagreeing with the market, the correct quant action is
usually to record the signal and not enter the trade.

The analyzer also prints `q`, a 0-100 trade-quality score. It combines
fee-adjusted edge, lower-bound edge, bid support, bid size, spread, model/market
disagreement, residual/ensemble agreement, time to close, and observed-high
context. It is an audit/ranking score, not a separate approval override.

By default, `analyze` ranks both BUY_YES and BUY_NO candidates. Use `--side yes`
or `--side no` to filter to one side. NO rows use Kalshi's reciprocal book: NO
entry ask is `no_ask`, NO exit bid is `no_bid`, and the public-market size proxy
maps NO bid size to YES ask size and NO ask size to YES bid size.

The `tail-basket` command is a separate paper-only scanner for the "market just
opened" use case. It finds the bucket closest to the WeatherEdge forecast, then
selects the lower and upper edge buckets that are fully outside the forecast
band, defaulting to forecast +/- 3F. It evaluates those edge buckets as BUY_NO
legs and optionally adds a smaller BUY_YES leg on the center bucket. The basket
can only place paper orders when:

- at least one far-tail NO leg passes the normal trade gates
- selected tail-bucket YES probability is below the configured maximum
- total sized basket spend is below the configured maximum
- every settlement-bucket scenario stays within the worst-case-loss cap

This is still not a live-money order bot. It is designed to create a clean paper
journal for the hypothesis that early next-day edge buckets are overpriced.

Every `analyze` run records a decision snapshot for each evaluated row,
including rejected rows. Those snapshots now include the forecast timestamp,
observed-high mode, intraday completion flag, event ticker, market status, and
market close time when available. `backtest-signals` defaults to strict
research sampling: it excludes rows that look post-resolution and keeps only
the latest row for each target date, market ticker, and side. Use
`--sample-mode all` only when you intentionally want to inspect every repeated
15-minute scan.

The walk-forward calibration backtest prints reliability buckets and temperature
cohorts. Over a large sample, rows where the model averages 0.6-0.7 should
resolve near 60-70% if the probability engine is calibrated, and hot-day cohorts
should not be hidden by normal-day aggregate scores.

For a stricter point-in-time check of the live blend, run:

```bash
python3 -m sfo_kalshi_quant.cli backtest-calibration --source clean-blend
```

That source uses only archived next-day blend snapshots made before the target
day and excludes same-day observed-high lock/floor rows. It is the better
answer to "could this have predicted past dates using only information known
then?"

The key model risk is target mismatch. Kalshi resolves from the NWS Daily
Climate Report for San Francisco Airport, not from Google, weather apps, or raw
hourly observations. During daylight saving time, the report date follows local
standard time, so the settlement day is not exactly the daylight
midnight-to-midnight calendar day.

Because of that, all trading-side day math runs on one fixed-PST settlement
clock (`sfo_kalshi_quant/settlement_day.py`, mirroring the forecaster's
`settlement_calendar.py`): `today`/`tomorrow`/`rolling` targets, the same-day
entry cutoff, and auto-settlement completion checks. During DST the civil and
settlement dates disagree between 00:00 and 01:00 PDT; the settlement clock
keeps the scanner from targeting or settling the wrong Kalshi day in that
window. The same-day entry cutoff hour (`PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR`,
default 14) is measured on that clock, i.e. 15:00 PDT in summer.

Paper entry holds one open position per market: an open YES (or NO) leg blocks
new entries on either side of that bucket. Holding both sides locks in the
combined entry costs plus fees, so the monitor's exit rules manage the open leg
instead of hedging it.

The `arbitrage` command is the explicit exception to that side-agnostic block.
It is paper-only and scans every active temperature bin for the target day
before placing anything. It evaluates three guaranteed-payoff structures:

- same-bin YES+NO boxes, approved only when `yes_cost + no_cost < 1`
- full-ladder BUY_YES sets, approved only when the active ladder is complete
  from lower tail to upper tail and `sum(yes_costs) < 1`
- full-ladder BUY_NO sets, approved only when the active ladder is complete
  and `sum(no_costs) < number_of_bins - 1`

Sizing is equal-contract dutching: every leg in the portfolio receives the same
whole-contract quantity so settlement payout is fixed by construction. The
calculator uses the same rounded Kalshi fee model as normal paper orders at the
final group size, caps size by visible ask depth, max contracts, configured
event risk, and optional `--max-arb-spend`, then places the group only after a
preflight confirms there is no existing open exposure in any affected market.
If a ladder is missing an active bin or has a gap/overlap, full-ladder
arbitrage is rejected rather than treated as hedged.

`backtest-signals` reports per-stream calibration (`weather_model`,
`market_prior`, `traded`) over the same settled rows. The traded posterior
blends the market prior in, so only a weather-model stream that beats the
market-prior stream is evidence of real alpha rather than market agreement.
Use `--sample-mode entry-per-market-side` to score the first approved snapshot
per market/side — the decision that actually opened the position — instead of
the latest scan; pair it with `backtest-market` for executed-order PnL.

For the research basis behind the side-aware design, see
[research_yes_no_strategy.md](research_yes_no_strategy.md).
