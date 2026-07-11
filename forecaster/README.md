# SFO Weather Forecaster

This project forecasts tomorrow's high temperature at San Francisco Airport. The
research model is trained on 10 years of NOAA hourly observations, while the live
website blends public forecast APIs with local SFO history and current airport
observations.

## What It Predicts

The headline target is the next **local calendar day high** at SFO. For each
hourly observation today, the model predicts the maximum temperature observed
tomorrow in Pacific time.

The project also trains a secondary target, spot temperature 24 hours ahead, as
a sanity check against a simpler persistence problem.

## Live Forecast Strategy

The live dashboard is intentionally forecast-first but not single-source:

1. **Google Weather API**: highest live weight by a small margin. It uses
   hourly forecasts and takes the max temperature across tomorrow's SFO local
   calendar date, then caches that result locally so the public website never
   exposes the API key. It can also fetch Google's daily forecast and current
   conditions; daily forecast is only a low-weight Google-internal cross-check
   unless the archive proves it should matter more.
2. **NWS / NOAA forecast grid**: nearly equal live weight. The page reads the
   official NWS grid forecast and uses `maxTemperature` when available.
3. **Open-Meteo forecast**: supporting live forecast source using a free,
   browser-accessible API for the SFO coordinates.
4. **SFO historical climatology**: low-weight stabilizer and fallback from the
   project's 10-year NOAA history table.
5. **Live airport observations**: current KSFO, KOAK, KSJC, KSQL, KPAO, and KHAF
   readings from NWS. They make a small capped adjustment when SFO is currently
   warmer or cooler than nearby airports.

The default blend is 38% Google Weather, 36% NWS, 18% Open-Meteo, and 8% SFO
history. If one live source is unavailable, the available sources are reweighted
automatically. Live airport observations are intentionally limited to a small
adjustment so today's microclimate does not overpower tomorrow's forecast.
After enough completed forecasts have been scored, the blend also learns
conservative source weights from the archive by nudging weight toward sources
with lower NWS-scored MAE. Until at least five scored days exist, it keeps the
configured base weights to avoid overfitting.

Google Weather is limited by a local **Weather event budget** through
`google_weather_cache.py`. The default budget is 8,000 events/month and 260
events/day, both below the 10,000 monthly free usage cap. The dashboard only
uses the cached Google value when it is for the current SFO tomorrow date and
less than 24 hours old.

Each successful Google refresh is also archived in `weather.db`:

- `forecast_google_daily_high`: one row per Google forecast snapshot, including
  the predicted high, target date, peak hour, actual high when available, and
  absolute error.
- `forecast_google_hourly`: the hourly Google forecast rows from each snapshot.
- `forecast_blend_daily_high`: one row per refresh for the final blended
  prediction. It stores Google, NWS, Open-Meteo, history, normalized weights,
  station adjustment, and the NWS-scored error when the day is complete.

One refresh requests 72 forecast hours from Google. The Weather API can return
that data in multiple paged HTTP responses, so the code tracks refresh snapshots
rather than pretending each snapshot is exactly one network request. The archive
keeps all returned hours and derives daily-high benchmark rows for every future
SFO local date with enough hourly coverage, not just tomorrow. This lets the
project evaluate which lead time was most accurate later.

Running `python google_weather_cache.py` without `--refresh` does not call the
Google API. It reuses the cached Google forecast, refreshes the free/public
NWS, Open-Meteo, and airport-observation context for the blend, archives the
current cache if needed, and updates benchmark scores for old forecasts whose
actual SFO high is now present in the historical table.

Ground truth comes from live NWS KSFO observations:

- `nws_station_observations`: archived observed temperatures from the official
  NWS station feed.
- `nws_daily_high_ground_truth`: current high-so-far for today and final daily
  highs for completed NWS/Kalshi local-standard report days.

Forecast scoring prefers `nws_daily_high_ground_truth` once a local day is
complete, then falls back to the historical NOAA table when needed.

The dashboard reports the blended forecast archive as a clean next-day track
record. The headline score uses the last eligible archived snapshot from the
SFO day before the target date, and excludes rows where the NWS observed-high
decision was `lock` or `floor`. Those same-day rows remain useful settlement
context, but they are not forecast-skill evidence. The supporting chart still
groups eligible refresh errors by Google refresh sequence, so the project can
check whether later prior-day snapshots are actually improving the answer.

The dashboard also reports a live confidence signal. It checks whether forecast
sources agree, how many nearby airport observations are fresh, and whether SFO
is currently warmer or cooler than nearby Bay Area stations. This makes the live
prediction more transparent without claiming a higher accuracy score before
archived forecast backtesting is complete.

## Automation Journey

The project started as a local ML/weather dashboard, but the live forecast is
now automated end to end:

1. **Forecast source**: Google Weather is the highest-weight live input. The
   API key is kept off the public website; refreshes happen server-side and are
   written to `google_weather_cache.json` plus the SQLite archive.
2. **Always-on runner**: an AWS EC2 Ubuntu arm64 instance runs the refresh
   workflow even when the laptop is asleep. There is no local launchd job; the
   cloud machine is the single automation source.
3. **Scheduled jobs**: systemd timers refresh NWS ground truth, fetch Google
   Weather within the event budget, and rebuild the blended forecast twice
   hourly from 05:10 through 18:40 PT and hourly overnight. The
   `sfo-operational-publish.timer` runs every five minutes to generate
   `trading_signal.json`, `cities_data.json`, and
   `publication_manifest.json`, validate the snapshot, and republish the site.
   The `sfo-strategy-lab-refresh.timer` runs every fifteen minutes as the
   research-only path that rebuilds `strategy_research.json`; it does not call
   the paid Google Weather refresh path. Strategy Lab data is plain public JSON
   containing only paper-trading research. The same server also runs the
   prediction-market paper scanner and exit monitor from the companion trading
   repo.
   Nightly NWP maintenance archives leads 1 and 2, the horizons used by the
   rolling live serve. Lead 3 is retained as a manual/on-demand historical
   backfill capability for research; it is intentionally not fetched by the
   scheduled `nwp_archive.py --daily` job.
4. **Public website**: after each successful rebuild, the EC2 server uses
   a GitHub deploy key with write access to publish the prebuilt React SPA and
   the fresh data JSONs to `gh-pages`, which GitHub Pages serves at
   `https://jaxsonb04.github.io/weather_edge/`.

In short: AWS fetches the paid Google Weather data privately, the forecaster
stores/scales it with the public NWS/Open-Meteo context, and GitHub Pages
publishes only the safe static output.

## Results

Per-day A/B test on the held-out forecast period:

| Target | LSTM MAE | XGBoost MAE | Persistence MAE | Result |
|---|---:|---:|---:|---|
| Tomorrow's daily high | **3.19°F** | 3.87°F | 3.79°F | LSTM wins |
| Spot temp 24h ahead | 1.94°F | 1.84°F | 1.84°F | No clear model win |

For point-in-time validation of the live blend archive, use:

```bash
python -m sfo_kalshi_quant.cli backtest-calibration --source clean-blend
```

That path uses only clean archived next-day blend forecasts that existed before
the target day started. Same-day observed-high lock/floor rows are excluded.

For tomorrow's high, the LSTM cuts error by **17.5%** versus XGBoost. The paired
A/B test gives a 95% confidence interval of **+0.47°F to +0.89°F** MAE
improvement, with p < 0.0001.

The spot-temperature target is intentionally less dramatic: the confidence
interval crosses zero, so the honest conclusion is that persistence/XGBoost are
already about as strong as the LSTM there.

## Why It Works

Weather at SFO has strong memory and seasonality. The pipeline turns raw hourly
observations into lag, rolling, calendar, pressure, humidity, wind, and cloud
features. XGBoost trains on 75 selected numeric features; the LSTM trains on 13
weather/calendar inputs and learns the lag structure from 48-hour sequences.

The LSTM performs better on the daily-high target because it sees the recent
temperature pattern as a sequence instead of a bag of engineered lag columns.

## Limitations

The model is weakest on rare hot days. That is visible in the error-by-range
plot: normal SFO days are well covered, but the 80°F+ tail has far fewer training
examples. The next accuracy improvement would come from adding nearby stations
or weighting/extending heatwave examples more aggressively.

Other limits:

- Single station: this predicts SFO Airport, not every Bay Area microclimate.
- 2020 is missing from the source archive, leaving a gap in the history.
- The website is static but can call public browser-accessible weather APIs.
  If live forecast requests fail, the widget falls back to date-based SFO
  climatology.

## File Map

```text
ab_test.py                       paired significance tests and bootstrap lift
cities.py                        canonical fifteen-city market/station registry
city_truth.py                    live and IEM-backed per-city CLI settlement truth
clisfo.py                        SFO NWS Daily Climate Report parser
combine_psv.py                   NOAA PSV files -> clean hourly CSV
compare_models.py                head-to-head metrics and calibration plots
eda.py                           exploratory plots
emos_forecast.py                 rolling multi-city EMOS fit and live serve
emos_recalibration.py            EMOS recalibration research helpers
features.py                      feature engineering and prediction targets
fetch_inland_history.py          inland-station history acquisition helper
forecast_backtest.py             clean next-day SFO blend backtest
forecast_postproc_backtest.py    multi-city post-processing comparison
forecast_scoring.py              forecast scoring and proper-score helpers
forecast_tomorrow.py             static month/day forecast lookup
forecast_validation.py           artifact and forecast validation checks
google_weather_cache.py          SFO live blend, cache, archive, and site JSON
load_to_db.py                    cleaned station CSV -> SQLite
lstm_model.py                    PyTorch LSTM training
nwp_archive.py                   point-in-time multi-model NWP archive
nws_ground_truth.py              NWS observations and daily-high truth
postproc_models.py               empirical/EMOS post-processing models
recalibration_replay.py          point-in-time recalibration replay
settlement_calendar.py           fixed-standard settlement-day calculations
xgboost_model.py                 XGBoost training, baselines, and diagnostics
```

`model_compare_results.json` and `ab_test_results.json` are retained committed
research outputs. They are reviewed together and manually used to produce
`public/diagnostics.json`; no production timer retrains models or regenerates
that public diagnostic automatically.

## Data Sources

- NWS API: https://api.weather.gov
- NWS gridpoint documentation: https://weather-gov.github.io/api/gridpoints
- NWS station observations: `https://api.weather.gov/stations/{station}/observations/latest`
- Google Weather hourly forecast API: https://developers.google.com/maps/documentation/weather/hourly-forecast
- Google Weather daily forecast REST API: https://developers.google.com/maps/documentation/weather/reference/rest/v1/forecast.days/lookup
- Google Weather current conditions REST API: https://developers.google.com/maps/documentation/weather/reference/rest/v1/currentConditions/lookup
- Google Maps Platform Environment API pricing: https://developers.google.com/maps/billing-and-pricing/pricing#environment-pricing
- Open-Meteo forecast API: https://open-meteo.com/en/docs
- NOAA GHCNh observations: source files in `2016-2026 weather data/`

## How To Run

```bash
python3 -m venv venv
source venv/bin/activate
pip install pandas numpy matplotlib seaborn scikit-learn xgboost torch scipy

python combine_psv.py --dir "2016-2026 weather data" --out combined_weather.csv
python load_to_db.py
python features.py
python xgboost_model.py
python lstm_model.py
python compare_models.py
python ab_test.py
python forecast_tomorrow.py
python nws_ground_truth.py --days 14
python google_weather_cache.py --refresh
```

Each step writes its outputs to disk so upstream data prep does not need to be
rerun for every experiment.

For the Google fetch, put `GOOGLE_WEATHER_API_KEY=...` in `.env` first. The
`.env` file is ignored by git.

Production refresh automation runs on EC2 (see
[`../docs/aws_deployment.md`](../docs/aws_deployment.md)); there is no local
launchd job. Scheduled NWP maintenance fetches leads 1 and 2; lead 3 remains a
manual/on-demand research backfill.

Google Weather usage is tracked in `.google_weather_usage.json` by billable
Weather events, not just refresh attempts. One enhanced refresh uses about five
Weather events by default:

- 3 events for 72 hours of `forecast.hours` data, requested in 24-hour pages.
- 1 event for `forecast.days`, used as a Google-internal daily-high cross-check.
- 1 event for `currentConditions`, saved as same-day context.

The default monthly event budget is 8,000, below the 10,000 monthly free usage
cap shown for Google Maps Platform Weather Usage. The 30-minute active-day
schedule is roughly 28 refreshes/day * 5 events * 31 days = 4,340 events/month.
You can tune the budget with environment variables:

```bash
export GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET=8000
export GOOGLE_WEATHER_DAILY_EVENT_BUDGET=260
export ENABLE_GOOGLE_DAILY_FORECAST=1
export ENABLE_GOOGLE_CURRENT_CONDITIONS=1
export GOOGLE_DAILY_INTERNAL_WEIGHT=0.15
```

`forecast.hours` remains the main Google input because Kalshi settles the SFO
local-calendar-day high. The daily endpoint can nudge the Google component, but
it is intentionally low-weight until the archive proves it improves error.
Google current conditions are context only; NWS/KSFO observations remain the
official same-day lock because they match settlement.

To score archived Google forecasts later without spending an API call:

```bash
python google_weather_cache.py
sqlite3 weather.db "SELECT target_date, predicted_high_f, actual_high_f, abs_error_f FROM forecast_google_daily_high ORDER BY target_date DESC;"
sqlite3 weather.db "SELECT target_date, predicted_high_f, actual_high_f, abs_error_f, calls_used_today FROM forecast_blend_daily_high ORDER BY fetched_at DESC;"
```

To inspect the official high-so-far / completed daily highs:

```bash
sqlite3 weather.db "SELECT local_date, high_f, is_complete FROM nws_daily_high_ground_truth ORDER BY local_date DESC;"
```
