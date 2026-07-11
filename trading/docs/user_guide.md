# Beginner User Guide

This guide assumes you know nothing about this project, weather prediction markets,
or quant trading. It also assumes you are running commands from the project
root:

```bash
cd /path/to/WeatherEdge
```

## What This Project Does

This project is a paper-trading research tool for daily-high prediction markets
across fifteen U.S. cities. SFO is the flagship; all cities share the registry,
portfolio allocator, settlement/finality rules, and paper journal.

It does four jobs:

1. Reads per-city forecasts and historical accuracy from `forecaster/`.
2. Converts each forecast into probabilities for its temperature buckets.
3. Compares those probabilities with live prediction-market bid/ask prices.
4. Records paper trades only when the opportunity passes the selected paper
   risk gates.

It does not place real-money orders. The scheduled EC2 scanner runs two paper
profiles (`live` and `research`) over `PAPER_CITIES=all`. It enters maker-first
with resting paper limits; a visible-ask crossing is only a proxy fill and does
not model queue position.

## Data Sources

The main forecast data comes from:

```text
forecaster/
```

The trading package reads these files/tables from the forecaster:

- `weather.db`: latest blended forecast, Google Weather fields, NWS fields,
  Open-Meteo fields, station observations, and Google hourly forecasts.
- `google_weather_cache.json`: fallback Google forecast if the DB blend is not
  available.
- `ab_test_results.json`: historical model predictions and actual outcomes used
  for calibration.

Google Weather is still being used. In this repo it enters through the upstream
forecaster's `google_high_f`, `forecast_google_hourly`, and fallback cache. The
current center forecast is the upstream blended `predicted_high_f`, not a direct
Google-only API call.

## Key Market Terms

`Kalshi`: A prediction market exchange. A weather contract pays $1 if the event
happens and $0 if it does not.

`Contract`: One yes/no bet on an event. Example: "SFO high is 68-69F today."

`YES`: The side that wins if the listed event happens.

`NO`: The side that wins if the listed event does not happen.

`Ask`: The cheapest current price where someone is willing to sell you YES. If
the YES ask is `0.14`, buying one contract costs about 14 cents plus fees.

`Bid`: The highest current price where someone is willing to buy YES from you.
If the YES bid is `0.13`, that is roughly where you could exit immediately.

`Spread`: `ask - bid`. A small spread is healthier. A wide spread means you may
lose money just entering and exiting.

`Depth` or `size`: How many contracts are available at a bid or ask. A 1-cent
ask with no bid depth can be a trap because you may not be able to exit.

`Liquidity`: A broad word for whether the market has enough bids, asks, and
size to trade realistically.

`Settlement`: The final official result. Each city uses its configured NWS Daily
Climate Report station/product and fixed-standard climate day.

`CLI`: The NWS climate report product used by the market settlement rules.

`KSFO`: San Francisco Airport weather station, the flagship station. Other city
markets use their own settlement stations from `cities.py`.

## Forecast Terms

`Forecast center`: The main predicted high temperature, in degrees Fahrenheit.

`Google high`: Google Weather's forecast high from the upstream forecaster.

`NWS high`: National Weather Service forecast high from the upstream forecaster.

`Open-Meteo high`: Open-Meteo forecast high from the upstream forecaster.

`History high`: A historical/climatology component from the upstream forecaster.

`Blend`: A combined forecast. The current project reads the upstream blend from
`forecast_blend_daily_high`.

`Ensemble`: A group of model runs. Instead of one forecast, an ensemble gives
many possible outcomes. This project fetches Open-Meteo GFS ensemble data and
uses 31 highs: one control forecast plus 30 members.

`Station-aligned ensemble`: The raw Open-Meteo grid can miss KSFO. This project
shifts the ensemble members so their center matches the SFO station-aligned
forecast, then uses the member spread and shape for probabilities.

`Observed high so far`: Today's highest observed KSFO temperature so far. If the
observed high is already 67F, any final bucket below 67F is impossible.

## Quant Terms

`Probability`: The model's estimate that a bucket will settle YES.

`Calibration`: Whether stated probabilities match reality over time. If the
model says 70% many times, those events should win about 70% of the time.

`Brier score`: A probability accuracy score. Lower is better.

`Log loss`: Another probability accuracy score. Lower is better and it punishes
overconfident wrong forecasts.

`Backtest`: Testing the strategy on past data. The calibration backtest checks
probability quality, not real trading profit.

`Paper trade`: A simulated trade recorded in SQLite. It uses real market prices
but does not send an order to Kalshi.

`Edge`: Expected profit per contract after estimated entry fee. Positive edge
means the model thinks the contract is underpriced.

`Lower-confidence edge` or `edge_lcb`: A more conservative edge using the lower
confidence probability. This guards against model uncertainty.

`Kelly sizing`: A math method for sizing bets based on edge and risk. This
project uses fractional Kelly, which is deliberately smaller.

`Bankroll`: Your paper account size. Example: `--bankroll 1000`.

`Daily budget`: Optional ad-hoc cap for manual runs (`--daily-budget 50`).
The scheduled AWS paper scanner runs without one; exposure is risk-gated by
per-position sizing, a per-target exposure cap, and one entry per market/side.

## Analyze Table Columns

When you run `analyze`, you will see rows for each temperature bucket.

Before the table, you may also see `forecast context`:

`lead`: How many hours ahead the forecast was made for that target day.

`fresh_stations`: How many nearby airport stations had fresh observations when
the upstream forecaster built the blend.

`google_events`: How many Google Weather billable events have been used today
and this month against the configured budgets. The forecaster now counts Weather
events, not just refresh attempts.

`google_hourly`, `google_daily`, and `google_current`: Extra Google Weather
context from the upstream forecaster. Hourly remains the settlement-aligned

`intra`: Same-day intraday probability. This is only available for today. It
uses the observed KSFO high-so-far, remaining hourly forecast, and time of day
to avoid treating near-boundary values as safe for the lower Kalshi bin.

`heat`: Estimated chance that the current observed bin gets exceeded later
today. Example: if the high-so-far is close to the next half-degree settlement
boundary at 1pm, this should warn you that the next bin still matters.
input; daily forecast and current conditions are cross-checks.

`weights`: The current blend weights. `G` is Google Weather, `NWS` is National
Weather Service, `OM` is Open-Meteo, and `Hist` is SFO history.

`weight_mode`: Whether the upstream forecaster is using base weights or learned
adaptive weights from scored forecast history.

`label`: The bucket, such as `66F to 67F` or `74F or above`.

`side`: The side being evaluated. `YES` wins if the bucket happens. `NO` wins
if the bucket does not happen.

`bid`: Current Kalshi bid for the displayed side. This is the approximate exit
price if you already hold that side.

`ask`: Current Kalshi ask for the displayed side. This is the approximate entry
price if buying that side.

`resid`: Probability from the calibrated residual model.

`ens`: Probability from the station-aligned ensemble. Shows `n/a` if ensemble
data is disabled or unavailable.

`model`: Weather-model probability after blending residual and ensemble inputs.

`mkt`: Market-implied probability from Kalshi bid/ask prices.

`p`: Final trading probability that the displayed side wins after cautiously
blending model and market prior.

`p_lcb`: Conservative lower-confidence probability.

`edge`: Expected profit per displayed-side contract after estimated entry fee.

`edge_lcb`: Conservative edge using `p_lcb`.

`q`: 0-100 trade quality score. It combines edge, lower-bound edge, bid support,
spread, model/market disagreement, ensemble agreement, time to close, and
observed-high context. It helps rank rows, but it does not override hard gates.

`contracts`: Recommended paper contracts if the row passes all gates.

`spend`: Paper dollars spent if placing approved paper trades.

`decision`: `TRADE` means the row passed the gates. `NO` means do not enter.

## Safety Gates

The strategy rejects trades when:

- The market is not active.
- The selected-side ask is not tradeable.
- There is no selected-side bid support.
- Bid size is too small.
- The spread is too wide.
- Model probability and market probability disagree too much.
- Final probability is too low.
- Edge is too low.
- Lower-confidence edge is too low.
- Today's observed high already makes the bucket impossible.
- A 1c/2c tail trade lacks exceptional evidence and liquidity.

A `NO` decision is normal. Most rows should be rejected.

## First-Time Workflow

Inspect all cities without recording orders:

```bash
python3 -m sfo_kalshi_quant.cli --no-color analyze --target-date rolling --side both --cities all
```

Run the actual maker-first portfolio path locally in dry paper mode:

```bash
PAPER_RISK_PROFILES=live,research PAPER_ENTRY_MODE=limit PAPER_CITIES=all \
  bash trading/deploy/aws/run_paper_scan_profiles.sh
```

Run the calibration check:

```bash
python3 -m sfo_kalshi_quant.cli --no-color backtest-calibration
```

This answers: "Do the model probabilities look believable historically?"

Then analyze today and tomorrow:

```bash
python3 -m sfo_kalshi_quant.cli --no-color analyze --target-date both
```

To include NO-side candidates too:

```bash
python3 -m sfo_kalshi_quant.cli --no-color analyze --target-date both --side both
```

If today's current observed high in your local DB is stale, override it:

```bash
python3 -m sfo_kalshi_quant.cli --no-color analyze --target-date today --observed-high 67
```

This means: "Assume today's official high so far is already 67F, so lower
buckets cannot win."

## Example Live Usage

"Live usage" here means live Kalshi market data and live/current forecast
artifacts, still paper-only.

Start with a clean analysis:

```bash
python3 -m sfo_kalshi_quant.cli --no-color analyze --target-date both
```

Example output shape:

```text
Kalshi market snapshot KXHIGHTSFO-26JUN03
forecast 2026-06-03: 67.82F source_spread=5.10F method=weighted Google + NWS + Open-Meteo + SFO history
intraday: observed_high_so_far=67.0F; latest_temp=66.2F
ensemble: station_mean=67.82F raw_mean=72.39F station_std=1.22F members=31 cell=land

side label          bid   ask resid  ens   model  mkt    p     p_lcb edge  edge_lcb q     contracts spend    decision
YES  68F to 69F     0.13  0.14 0.229 0.548 0.325 0.140 0.226 0.030   0.076  -0.120  33.3    0.0000 $   0.00 NO
YES  66F to 67F     0.03  0.04 0.293 0.387 0.321 0.036 0.169 0.000   0.119  -0.050  40.5    0.0000 $   0.00 NO
YES  65F or below   0.00  0.01 0.000 0.000 0.000 0.000 0.000 0.000  -0.020  -0.020   0.0    0.0000 $   0.00 NO
```

How to read that:

- The forecast center is `67.82F`.
- The ensemble has `31` members.
- `65F or below` is impossible because observed high is already at least 67F.
- The 68-69F row has positive raw edge, but its conservative edge is negative,
  so the decision stays `NO`.
- No paper trade is placed unless the decision says `TRADE`.

If you want to paper trade approved rows with a $350 bankroll and max $50 for
the day:

```bash
python3 -m sfo_kalshi_quant.cli --no-color --bankroll 1000 analyze --target-date today --place-paper
```

If there are no approved rows, it spends `$0`. That is a successful defensive
result.

If you want to evaluate both YES and NO paper opportunities:

```bash
python3 -m sfo_kalshi_quant.cli --no-color --bankroll 1000 analyze --target-date today --side both --place-paper
```

Check paper positions:

```bash
python3 -m sfo_kalshi_quant.cli --no-color paper-report
```

If you want to manually paper-buy one specific NO row:

```bash
python3 -m sfo_kalshi_quant.cli --no-color paper-buy --ticker KXHIGHTSFO-26JUN03-B68.5 --side no --amount 10
```

If you have an open paper order and want to exit before settlement:

```bash
python3 -m sfo_kalshi_quant.cli --no-color paper-close --order-id 1
```

That uses the live Kalshi bid for the stored side as the simulated sell price.

After the final CLISFO high is known, settle paper trades:

```bash
python3 -m sfo_kalshi_quant.cli --no-color paper-settle --target-date 2026-06-03 --settlement-high 67
```

Then review performance:

```bash
python3 -m sfo_kalshi_quant.cli --no-color backtest-market
python3 -m sfo_kalshi_quant.cli --no-color backtest-signals
```

## Daily Routine

Use this sequence:

```bash
python3 -m sfo_kalshi_quant.cli --no-color backtest-calibration
python3 -m sfo_kalshi_quant.cli --no-color analyze --target-date both
python3 -m sfo_kalshi_quant.cli --no-color --bankroll 1000 analyze --target-date today --place-paper
python3 -m sfo_kalshi_quant.cli --no-color paper-report
python3 -m sfo_kalshi_quant.cli --no-color backtest-signals
```

If your observed high is fresher than the local DB:

```bash
python3 -m sfo_kalshi_quant.cli --no-color --bankroll 1000 analyze --target-date today --observed-high 67 --place-paper
```

## What To Trust

Trust the official settlement rules first. Kalshi resolves from the NWS Daily
Climate Report for San Francisco Airport, not from a generic city forecast.

Trust Google Weather as an important forecast input because it has been strong
for this workflow, but still station-check it against KSFO and calibration
history. A good forecast source can still be wrong, stale, or mismatched to the
settlement station.

Trust `edge_lcb` more than raw `edge`. Raw edge can look exciting; lower-bound
edge is the adult in the room.

Trust `decision=NO` rows. Doing nothing is part of the strategy.

## What Not To Do

Do not buy 1-cent contracts just because the model probability is above 1%.

Do not treat multiple SFO temperature buckets as diversification. They are the
same event, and only one bucket can win.

Do not override `--observed-high` lower than the true high so far.

Do not conclude the strategy works from one day or one week. You need many
settled markets and calibration checks.

Do not mistake paper trading for live trading. This repo currently has no
real-money order placement.
