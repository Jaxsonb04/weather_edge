# WeatherEdge Prediction-Market Engine

Paper-trading and backtesting engine for daily-high temperature prediction
markets across fifteen U.S. cities. SFO is the flagship and retains the deepest
Google/NWS/Open-Meteo/LSTM blend; the other fourteen cities use the shared
NWP→EMOS→CLI pipeline.

In WeatherEdge, this module reads forecast artifacts from:

```text
/path/to/WeatherEdge/forecaster
```

It reads the per-city forecast, applies each station's settlement clock and
same-day observed-high constraints, blends weather and market-implied
probabilities, subtracts estimated fees/spread, and records paper-only trades.
The default city selection is all registered markets (`PAPER_CITIES=all`).

Command output is color-coded by default:

- green: approved/positive
- red: rejected/negative
- yellow: caution/no active market
- cyan: section headers

Use `--no-color` before the command name if you want plain text:

```bash
python3 -m sfo_kalshi_quant.cli --no-color analyze --target-date rolling --side both --cities all
```

## Quick Start

If you are new to this project or to quant/weather trading, start with the
beginner guide: [docs/user_guide.md](docs/user_guide.md).
For the side-aware YES/NO research basis, read
[docs/research_yes_no_strategy.md](docs/research_yes_no_strategy.md).

```bash
python3 -m sfo_kalshi_quant.cli backtest-calibration
python3 -m sfo_kalshi_quant.cli backtest-calibration --source clean-blend
python3 -m sfo_kalshi_quant.cli analyze --target-date rolling --side both --cities all
python3 -m sfo_kalshi_quant.cli --risk-profile live portfolio-scan --target-date rolling --side both --cities all
PAPER_RISK_PROFILES=live,research PAPER_ENTRY_MODE=limit PAPER_CITIES=all bash deploy/aws/run_paper_scan_profiles.sh
python3 -m sfo_kalshi_quant.cli paper-report
python3 -m sfo_kalshi_quant.cli paper-close --order-id 1
python3 -m sfo_kalshi_quant.cli paper-auto-settle
python3 -m sfo_kalshi_quant.cli backtest-market
python3 -m sfo_kalshi_quant.cli backtest-signals
```

The scheduled entry path is `portfolio-scan`, not a legacy single-market
diagnostic. It evaluates all configured cities through the shared allocator.
Production paper entry is maker-first: `PAPER_ENTRY_MODE=limit` records a
resting limit at the reservation price, and the monitor marks a proxy fill only
when the visible ask crosses. That proxy does not model queue position.

Use `--target-date both` to show today's tradable market and tomorrow's
probability forecast in one run.

## What Is Implemented

- Kalshi public event/orderbook client.
- Per-city adapter for `weather.db`; SFO additionally uses
  `google_weather_cache.json` and `ab_test_results.json`.
- Reads extended forecaster metadata: lead hours, source weights, fresh station
  count, Google refresh usage, and observed-high lock details.
- Conditional residual calibration from historical LSTM forecast errors.
- Hybrid empirical/normal bucket probability model.
- Intraday high-so-far update from the forecaster's KSFO observations and
  latest Google hourly forecast.
- Same-day boundary-aware probability apportioning. If the observed or expected
  high is near the next Kalshi settlement bin before the day has cooled, the
  intraday model shifts probability toward the next bin instead of treating a
  value like 69.9°F as safe for the lower bucket.
- Station-aligned Open-Meteo GFS ensemble input. The parser keeps the control
  high plus 30 ensemble members, shifts the member distribution to the SFO
  station-centered forecast, and warns when nearby grid choices disagree.
- Market-aware posterior probability using live Kalshi bid/ask prices as a
  normalized, liquidity-aware market prior.
- Kalshi market-consensus forecast: the full de-vigged bin ladder distilled into
  the crowd's implied high temperature, modal bin, implied spread, and
  P10/P50/P90 — the forecast "people who put money" actually trade against. It
  is always surfaced (a `kalshi consensus:` line under the model forecast in
  `analyze`, a `market_consensus` block in the daily report, and the Strategy Lab
  dashboard) so the model-vs-market gap is visible at a glance. The `research`
  profile additionally *anchors* it harder into the blend and runs a guard that
  haircuts position size when the model bets hard against a confident, liquid
  market; both are OFF on `live` pending a walk-forward backtest (see
  `StrategyConfig.market_consensus_*`).
- Conservative Kalshi quadratic fee estimate.
- Fee-adjusted edge, lower-confidence-bound edge, trade-quality score, and
  fractional-Kelly sizing.
- Side-aware BUY_YES and opt-in BUY_NO evaluation with side-aware paper
  settlement and early close.
- Forecast-centered tail basket scan: paper BUY_NO on far edge buckets outside
  the forecast band plus a smaller center BUY_YES only when basket spend,
  modeled tail probability, and worst-case-loss guardrails pass.
- Decision-snapshot journal for every analyzed YES/NO row, including rejected
  rows, so signal quality can be backtested later.
- `backtest-signals` scores the weather model, market prior, and traded
  posterior as separate probability streams, and `--sample-mode
  entry-per-market-side` backtests the first approved snapshot per market/side
  — the actual entry decision — instead of the latest scan.
- All target-date, entry-gate, and auto-settle math uses each city's configured
  fixed-standard settlement clock and NWS CLI product.
- Paper stake overrides are capped by visible top-of-book ask size.
- Liquidity and sanity gates: no zero-bid penny tails, no huge model/market
  disagreement, no impossible same-day buckets, and no 1c/2c tail trades unless
  liquidity and evidence are exceptional.
- SQLite paper-trading journal.
- Walk-forward calibration backtest, including an optional clean archived-blend
  source that only uses forecasts made before the target day.
- Read-only `daily-report` JSON for public dashboard paper-research summaries.
- CLISFO parser scaffold for settlement auditing.

## Safety

There is no live order placement code. API keys are intentionally not required
for this phase.

Before live trading is considered, this project should accumulate enough market
snapshots and CLISFO settlements to run a true entry-price PnL backtest.

## Useful Commands

Collect a forecast and market snapshot:

```bash
python3 -m sfo_kalshi_quant.cli collect --target-date both
```

Analyze all active city markets (or narrow with `--cities sfo,lax`):

```bash
python3 -m sfo_kalshi_quant.cli analyze --target-date rolling --side both --cities all
python3 -m sfo_kalshi_quant.cli portfolio-scan --target-date rolling --side both --cities all
python3 -m sfo_kalshi_quant.cli --risk-profile research analyze --target-date rolling --side both --cities all
PAPER_RISK_PROFILES=live,research PAPER_ENTRY_MODE=limit PAPER_CITIES=all bash deploy/aws/run_paper_scan_profiles.sh
```

`tail-basket` and `arbitrage` remain diagnostic research commands. The scheduled
scanner uses the portfolio allocator, which considers:

```text
far edge bucket below forecast band -> BUY_NO if it clears normal gates
far edge bucket above forecast band -> BUY_NO if it clears normal gates
bucket closest to forecast center    -> small BUY_YES if it clears normal gates
```

Defaults are intentionally small: `$5` paper stake per approved tail NO and
`$1` on the center YES. The basket rejects itself if selected tail probability
is above `0.20`, sized spend exceeds `$12`, or any settlement bucket would lose
more than `$8`. It is not the active scheduled entry path.

Risk profiles:

- `live` (default): the stricter paper book and real-money-intent evidence
  record. It remains paper-only, requires positive lower-bound edge, applies
  SFO evidence gates where available, and concentrates entries in the
  researched favorite-price band.
- `research`: the single data collector. Loosest gates (so it approves the
  widest opportunity set) at the smallest size (so a bad idea stays tiny). It
  records the full opportunity set including center bins (comfort-edge off) so
  the readiness rescore can validate the `live` config. Paper-only; never a
  real-money candidate.

The strict `StrategyConfig()` baseline still exists internally for tests, but is
not a selectable CLI/env profile. Legacy names map onto the two profiles as
aliases on the CLI and in stored data: `balanced`/`conservative` -> `live`;
`exploratory`/`fast-feedback`/`fast` -> `research`.

When `PAPER_RISK_PROFILES=live,research`, the scheduled scanner runs exactly
those two books across all configured cities. Orders are profile- and
series-tagged, so books and city exposure remain isolated in one journal.

For today's market, the program automatically reads the forecaster database
for KSFO's observed high so far. If the Kalshi app, NWS page, or a fresher source
shows a newer current high than your local database, pass it manually with
`--observed-high`. Example: `--observed-high 67` means today's final high cannot
settle in any bucket below 67°F.

Record approved paper trades:

```bash
python3 -m sfo_kalshi_quant.cli analyze --target-date today --paper-stake 10 --place-paper
```

That means "spend $10 of paper money on each approved trade."

Close/sell one open paper trade before final settlement:

```bash
python3 -m sfo_kalshi_quant.cli paper-close --order-id 1
```

This uses the live Kalshi bid for the stored paper side. `paper-sell` is an
alias for the same command.

Run the probability calibration backtest:

```bash
python3 -m sfo_kalshi_quant.cli backtest-calibration --min-train 180
python3 -m sfo_kalshi_quant.cli backtest-calibration --source clean-blend --min-train 5
```

The calibration output includes probability buckets. Read those as a reliability
table: if the model often says 0.6-0.7, the observed win rate in that row should
eventually land near 0.6-0.7 over a large enough sample.

The default `lstm` source validates probability calibration on held-out model
outcomes. The `clean-blend` source validates archived live-blend snapshots that
existed before the target day and excludes same-day observed-high lock/floor
rows.

Run the recorded decision-signal backtest after official SFO highs are present:

```bash
python3 -m sfo_kalshi_quant.cli backtest-signals
python3 -m sfo_kalshi_quant.cli backtest-signals --min-quality 60
python3 -m sfo_kalshi_quant.cli backtest-signals --approved-only
```

This scores every recorded decision snapshot, including rejected rows unless
`--approved-only` is passed. It reports Brier score, log loss, approval rate,
approved paper PnL, and quality-bucket diagnostics.

Run tests without installing extra packages:

```bash
python3 trading/tests/run_tests.py
```

Settle paper orders once the final CLISFO value is known:

```bash
python3 -m sfo_kalshi_quant.cli paper-settle --target-date YYYY-MM-DD --settlement-high 67
python3 -m sfo_kalshi_quant.cli backtest-market
```

`--settlement-high 67` means "the official final SFO high temperature was
67°F." Replace `67` with the real CLISFO/Kalshi settlement value for that date.

## Beginner Paper-Trading Playbook

Use this like a research notebook with guardrails.

### 1. Start with a calibration check

```bash
python3 -m sfo_kalshi_quant.cli backtest-calibration
```

This asks: "Does the model put reasonable probabilities on the winning
temperature bucket?" It does not prove trading profit, but if this looks bad,
you should not trade.

### 2. Look at today and tomorrow

```bash
python3 -m sfo_kalshi_quant.cli analyze --target-date both
```

Today's section usually has real Kalshi bid/ask prices. Tomorrow's section may
only show probabilities if Kalshi has not listed the event yet.

Important columns:

- `model`: weather-model probability after residual, ensemble, and same-day
  intraday adjustment.
- `mkt`: normalized market-implied probability from Kalshi bid/ask prices.
- `intra`: same-day boundary/remaining-heat probability. `n/a` means tomorrow or
  no intraday data.
- `heat`: estimated chance the current observed bin gets exceeded later today.
- `side`: the evaluated side, `YES` or `NO`.
- `p`: final blended probability that the displayed side wins.
- `p_lcb`: conservative lower-confidence probability.
- `ask`: paper entry price if buying the displayed side.
- `edge`: expected profit per contract after estimated entry fee.
- `edge_lcb`: conservative edge using `p_lcb`.
- `q`: 0-100 trade quality score combining edge, lower-bound edge, bid support,
  spread, model/market disagreement, ensemble agreement, time to close, and
  observed-high context.
- `contracts`: recommended paper size.
- `decision`: `TRADE` means it passes the current paper-trading gates.

Cheap does not mean good. A 1-cent ask with no bid on the displayed side is
normally rejected because there is no exit support and the market is telling us
the row is a tail. The row may still show a theoretical model edge, but the
decision should remain `NO` unless liquidity, lower-bound edge, model/market
agreement, and ensemble evidence all clear the stricter cheap-tail gate.

By default the analyzer ranks both BUY_YES and BUY_NO candidates. To focus on a
single side, pass `--side yes` or `--side no`:

```bash
python3 -m sfo_kalshi_quant.cli analyze --target-date both --side yes
```

### 3. Place paper trades

```bash
python3 -m sfo_kalshi_quant.cli analyze --target-date today --paper-stake 10 --place-paper
```

This records only approved `TRADE` rows and spends $10 of paper money on each
one. It does not send orders to Kalshi.

For paper buy-limit simulation, use `--paper-entry-mode limit` or set
`PAPER_ENTRY_MODE=limit`. The analyzer records a resting paper limit when the
lower-confidence reservation price is below the visible ask; resting limits do
not settle or monitor as filled positions.

For tomorrow:

```bash
python3 -m sfo_kalshi_quant.cli analyze --target-date tomorrow --paper-stake 10 --place-paper
```

If there is no active Kalshi market yet, it records no trade.

### 4. View your paper positions

```bash
python3 -m sfo_kalshi_quant.cli paper-report
```

Look for the `id` shown at the start of each row. That id is what you use to
close/sell an open paper trade.

### 5. Close or sell before settlement

If you bought a paper position and want to exit before final settlement, close it:

```bash
python3 -m sfo_kalshi_quant.cli paper-close --order-id 1
```

The program fetches the current live Kalshi bid for the stored side and uses
that as your paper sell price. This keeps the position paper-money while using
real market prices.
Do not use `--exit-price` in normal use; it exists only as an offline override
when network data is unavailable.

Paper close math:

```text
realized_pnl = contracts * (live_side_bid - exit_fee - original_cost)
```

So if you bought 10 contracts at a total cost of 0.13 each and later sell at a
live side bid of 0.30 with a 0.01 exit fee, paper PnL is:

```text
10 * (0.30 - 0.01 - 0.13) = $1.60
```

You can also let the paper monitor close open positions when the live bid
implies a stop-loss or take-profit threshold:

```bash
python3 -m sfo_kalshi_quant.cli --no-color paper-monitor \
  --yes-take-profit-pct 50 --yes-stop-loss-pct 25 \
  --no-take-profit-pct 35 --no-stop-loss-pct 35 \
  --model-veto-max-loss-pct 60 --model-veto-buffer 0.08
```

The unrealized ROI math is:

```text
net_exit = live_side_bid - estimated_exit_fee
unrealized_roi = (net_exit - original_cost) / original_cost
```

This is still paper-only. It records an early paper close; it does not place a
real Kalshi sell order.

### 6. Settle at the final CLISFO value

If you hold the paper trade to final resolution, wait for the official NWS Daily
Climate Report value. Then run:

```bash
python3 -m sfo_kalshi_quant.cli paper-settle --target-date 2026-06-03 --settlement-high 67
```

Here `67` is not money and not a probability. It is the final official high
temperature in degrees Fahrenheit. If the final CLISFO high was 71°F, use
`--settlement-high 71`.

If you bought YES, the paper trade pays $1 per contract when the final high
lands in that bucket. If you bought NO, it pays $1 when the final high does not
land in that bucket.

### 7. Review paper performance

```bash
python3 -m sfo_kalshi_quant.cli backtest-market
```

This summarizes closed or settled paper trades: realized PnL, ROI, hit rate,
and average modeled edge.

### Daily Routine

```bash
python3 -m sfo_kalshi_quant.cli backtest-calibration
python3 -m sfo_kalshi_quant.cli analyze --target-date both
python3 -m sfo_kalshi_quant.cli analyze --target-date today --observed-high 67
python3 -m sfo_kalshi_quant.cli analyze --target-date today --paper-stake 10 --place-paper
python3 -m sfo_kalshi_quant.cli paper-report
```

Paper bankroll and stake examples:

```bash
# Use a $500 paper bankroll for risk recommendations.
python3 -m sfo_kalshi_quant.cli --bankroll 500 analyze --target-date both

# Paper bet exactly $5 per approved trade.
python3 -m sfo_kalshi_quant.cli analyze --target-date today --paper-stake 5 --place-paper

# Paper bet exactly $25 per approved trade.
python3 -m sfo_kalshi_quant.cli analyze --target-date today --paper-stake 25 --place-paper

# Use exactly $50 total for the day, split across all approved trades.
python3 -m sfo_kalshi_quant.cli --bankroll 1000 analyze --target-date today --place-paper

# Same, but force today's high-so-far to 67F if your local DB is stale.
python3 -m sfo_kalshi_quant.cli --bankroll 1000 analyze --target-date today --observed-high 67 --place-paper
```

Paper trading has no daily spend budget. Exposure is risk-gated instead:
per-position Kelly/risk sizing, a cumulative per-target exposure cap
(`max_target_exposure_pct` of bankroll, persisted across scans in the DB), and
at most one recorded entry per market/side per target date. If it finds zero
approved trades, it spends $0. That is a valid outcome.

The `--daily-budget` flag still exists for ad-hoc experiments, but the AWS
scanner does not use it.

### One-Week $1,000 Bankroll Plan

Hypothetical setup:

```text
Starting paper bankroll: $1,000
Daily paper budget:      none (risk-gated exposure)
Measurement period:      7 calendar days
```

Day 1 through Day 7, run:

```bash
python3 -m sfo_kalshi_quant.cli --bankroll 1000 analyze --target-date today --place-paper
python3 -m sfo_kalshi_quant.cli paper-report
```

During the day, optionally close open paper trades using the live Kalshi bid:

```bash
python3 -m sfo_kalshi_quant.cli paper-close --order-id 1
```

After final CLISFO settlement for each day, settle any remaining open trades:

```bash
python3 -m sfo_kalshi_quant.cli paper-settle --target-date YYYY-MM-DD --settlement-high FINAL_HIGH
```

At the end of the week, measure only that week:

```bash
python3 -m sfo_kalshi_quant.cli --bankroll 350 backtest-market --since 2026-06-03 --until 2026-06-09
python3 -m sfo_kalshi_quant.cli paper-report --since 2026-06-03 --until 2026-06-09
```

The weekly summary counts closed/settled trades as realized PnL and separately
shows any still-open paper capital at risk. `ending_bankroll_realized` is:

```text
starting bankroll + realized PnL
```

Later in the day, if the market moves:

```bash
python3 -m sfo_kalshi_quant.cli paper-close --order-id 1
```

After final settlement:

```bash
python3 -m sfo_kalshi_quant.cli paper-settle --target-date YYYY-MM-DD --settlement-high FINAL_HIGH
python3 -m sfo_kalshi_quant.cli backtest-market
```

## Next Research Tasks

- Backfill CLISFO settlements and compare them with the prior KSFO local-day
  ground truth.
- Archive Kalshi orderbooks several times per day.
- Add a market-PnL backtest using only data available at decision time.
- Add source-specific calibration for Google, NWS, Open-Meteo, and the blend.
- Add hot-tail regime features before any live-money phase.
