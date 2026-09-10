# WeatherEdge Forecaster

This directory owns both layers of WeatherEdge forecasting:

- the shared operational path: an eight-member NWP archive and rolling-origin
  EMOS forecast for all fifteen stations; and
- the **San Francisco flagship** research path: Google/NWS/Open-Meteo inputs,
  an LSTM trained on ten years of NOAA observations, and marine-layer features.

SFO is blend-capable, but the public operational point forecast may fall back to
the same EMOS weighted mean used by the other cities when optional inputs are
unavailable or do not pass freshness gates. The public artifact names the method
actually served. See the [project README](../README.md) for the whole system.

## What It Predicts

The operational target is each registered station's **local-standard climate-day
high**. City, station, timezone, market series, and official NWS settlement
product are defined together in `cities.py`.

The SFO LSTM research target is the next local calendar-day high at KSFO. For
each hourly observation today, the model predicts the maximum temperature
observed tomorrow in Pacific time.

The project also trains a secondary target, spot temperature 24 hours ahead, as
a sanity check against a simpler persistence problem.

## Operational Forecast Path

`nwp_archive.py` preserves point-in-time forecasts from eight NWP members.
`emos_forecast.py` fits rolling-origin post-processing per station and publishes
a calibrated Gaussian mean and spread. `city_truth.py` supplies station-keyed
settlement truth from final NWS Climatological Reports. Scheduled maintenance
archives the operational lead-one and lead-two horizons; lead three remains an
explicit historical-backfill and research capability.

The trading adapter and public dashboard consume the method recorded in the
current artifact. They do not assume that SFO's optional blend is available.

### SFO legacy blend and research layers

The deeper SFO path can combine:

1. **Google Weather API**: highest configured weight in the legacy blend. It uses
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

The legacy configured blend is 38% Google Weather, 36% NWS, 18% Open-Meteo, and
8% SFO history. Those weights describe that optional SFO path, not the shared
fifteen-city EMOS method or a guarantee about today's served forecast. Missing
sources are reweighted automatically, live-station adjustments are capped, and
the published method label remains the authority for what reached the dashboard.

Google Weather is limited by a local **Weather event budget** through
`google_weather_cache.py`. Monthly and daily ceilings are explicit operator
configuration, and the dashboard accepts a cached Google value only for the
expected SFO target date while it remains fresh.

## Apple WeatherKit Shadow Source

`apple_weatherkit.py` is an optional, all-city WeatherKit REST source. It asks
for hourly and daily forecasts together at four fixed UTC vintages/day, derives
complete 24-hour highs in each station's fixed-standard settlement window, and
keeps only the current normalized values in a private mode-0600 tmpfs cache.
Apple's daily maximum is a diagnostic; the settlement contributor is always
derived from hourly points. Only complete future settlement days are cached;
same-day elapsed hours are not reconstructed from forecast data.

This is not a live blend input. Its weight is zero and it never writes to
`weather.db`, `nwp_model_forecasts`, training archives, decision snapshots, or
public JSON. Provider expiry is a hard boundary, with a separate ten-minute
purge safety net. Historical scoring and promotion are deferred under the
current Apple Weather data-storage terms. See
[`docs/APPLE-WEATHERKIT.md`](../docs/APPLE-WEATHERKIT.md) for the complete
runtime and licensing boundary.

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

Intraday context and final settlement truth are intentionally separate:

- `nws_station_observations`: archived observed temperatures from the official
  NWS station feed.
- `nws_daily_high_ground_truth`: observation-derived high-so-far. These rows
  remain nonfinal even after the calendar day ends; elapsed time alone cannot
  prove that late or corrected observations are complete.
- `cli_settlements`: final NWS Climatological Report (Daily) highs, keyed by
  settlement station and local-standard date. Only `is_final = 1` rows may
  supply exact settlement truth.

Forecast scoring and exact intraday finality prefer final `cli_settlements`,
then use the historical NOAA table where that workflow explicitly permits it.

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

1. **Forecast source**: the shared operational baseline is the all-city
   NWP→EMOS path. SFO can add optional legacy-blend inputs when they are fresh
   and admitted; Google credentials and cached values remain server-side and
   never reach the public website.
2. **Always-on runner**: an AWS EC2 Ubuntu arm64 instance runs the refresh
   workflow even when the laptop is asleep. There is no local launchd job; the
   cloud machine is the single automation source.
3. **Scheduled jobs**: systemd timers refresh NWS ground truth, fetch Google
   Weather within the event budget, and rebuild the blended forecast twice
   hourly from 05:10 through 18:40 PT and hourly overnight. The
   `sfo-operational-publish.timer` runs every five minutes to generate
   `trading_signal.json`, `cities_data.json`, and
   `publication_manifest.json`, validate the snapshot, and republish the site.
   The `sfo-strategy-lab-refresh.timer` runs on a fixed wall-clock five-minute
   cadence as the
   bounded research-only path that rebuilds `strategy_research.json`; it does
   not call the paid Google Weather refresh path or rescan the full decision
   journal. Slow historical rescores carry their own `analysis_generated_at`
   timestamp in `strategy_analysis_cache.json`. The full rescore runs once after
   a deploy has restored production, against the verified immutable backup
   snapshot; the frequent path accepts the cache only when its source SHA and
   effective configuration fingerprint still match. Strategy Lab data
   is plain public JSON containing only paper-trading research. The same server also runs the
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

In short: AWS runs the station-aligned NWP/EMOS pipeline, keeps optional provider
data private, settles from official NWS truth, and publishes only validated
static artifacts to GitHub Pages.

## Results

The SFO studies contain two related but distinct summaries; they should not be
silently mixed.

| Evaluation | n | LSTM MAE | Comparator MAE | Result |
|---|---:|---:|---:|---|
| Paired LSTM vs XGBoost daily-high comparison | 442 days | **3.12°F** | 3.71°F | 15.8% lower MAE; Diebold–Mariano p < 0.001; LSTM wins 63% of days |
| Separate daily-high baseline summary | 442 days | **3.30°F** | Persistence 3.97°F | LSTM improves on the baseline |

Regenerate and inspect the source artifacts with `research/ab_test.py` and
`research/compare_models.py`. The daily-high evaluation uses one observation per
calendar day. The separate 24-hour spot-temperature experiment is retained as a
sanity check, but its hourly rows are autocorrelated, so its nominal p-value is
not quoted as independent daily evidence.

For point-in-time validation of the live blend archive, use:

```bash
python -m sfo_kalshi_quant.cli backtest-calibration --source clean-blend
```

That path uses only clean archived next-day blend forecasts that existed before
the target day started. Same-day observed-high lock/floor rows are excluded.

These SFO-only results are research evidence. They do not establish equal skill
for the other fourteen cities, and the live SFO artifact can still serve the
shared EMOS fallback when optional inputs are absent.

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
cities.py                        canonical fifteen-city market/station registry
city_truth.py                    live and IEM-backed per-city CLI settlement truth
clisfo.py                        SFO NWS Daily Climate Report parser
emos_forecast.py                 rolling multi-city EMOS fit and live serve
emos_recalibration.py            EMOS recalibration research helpers
emos_sources.py                  rolling-origin archive version-selection policy
forecast_backtest.py             clean next-day SFO blend backtest
forecast_postproc_backtest.py    multi-city post-processing comparison
forecast_scoring.py              forecast scoring and proper-score helpers
google_weather_cache.py          stable SFO refresh CLI and import facade
google_api.py                    Google fetch/parsing and paid-event budget ledger
blend_sources.py                 public source adapters and final blend assembly
blend_learners.py                walk-forward weight, MOS, and residual learners
blend_archive.py                 cache archive schema, migrations, and scoring
weather_cache_config.py          shared SFO blend paths and control settings
nwp_archive.py                   point-in-time multi-model NWP archive
nws_ground_truth.py              NWS observations and daily-high truth
postproc_models.py               empirical/EMOS post-processing models
recalibration_replay.py          point-in-time recalibration replay
settlement_calendar.py           fixed-standard settlement-day calculations
research/                        offline-only data preparation, training, and evaluation
research/ab_test.py              paired significance tests and bootstrap lift
research/combine_psv.py          NOAA PSV files -> clean hourly CSV
research/compare_models.py       head-to-head metrics and calibration plots
research/eda.py                  exploratory plots
research/features.py             feature engineering and prediction targets
research/fetch_inland_history.py inland-station history acquisition helper
research/forecast_tomorrow.py    static month/day forecast lookup
research/forecast_validation.py  artifact and forecast validation checks
research/load_to_db.py           cleaned station CSV -> SQLite
research/lstm_model.py           PyTorch LSTM training
research/xgboost_model.py        XGBoost training, baselines, and diagnostics
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

python research/combine_psv.py --dir "2016-2026 weather data" --out combined_weather.csv
python research/load_to_db.py
python research/features.py
python research/xgboost_model.py
python research/lstm_model.py
python research/compare_models.py
python research/ab_test.py
python research/forecast_tomorrow.py
python nws_ground_truth.py --days 14
python google_weather_cache.py --refresh
```

Each step writes its outputs to disk so upstream data prep does not need to be
rerun for every experiment. Keep `forecaster/` as the working directory; the
`research/` tools deliberately write the same project-relative artifacts as
before the package move. Lightweight helpers are also importable as
`research.features` and `research.forecast_validation`.

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

To inspect the nonfinal observed high-so-far and final CLI truth separately:

```bash
sqlite3 weather.db "SELECT local_date, high_f, is_complete FROM nws_daily_high_ground_truth ORDER BY local_date DESC;"
sqlite3 weather.db "SELECT local_date, max_temperature_f, is_final FROM cli_settlements WHERE station_id='KSFO' ORDER BY local_date DESC;"
```
