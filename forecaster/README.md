# WeatherEdge Forecaster

This directory owns shared station-aligned NWP/EMOS forecasting, final CLI
settlement truth, optional provider runtime adapters, and the separate SFO
historical ML research tree. The current source registry contains twenty
stations. The October 5 production audit found fifteen deployed stations on an
older backend; check runtime provenance before describing current coverage.

## Operational target and serving

The trading target is each station's daily-high temperature over its
**fixed-standard climate day**. Civil daylight-saving dates are not interchangeable
with that window. `cities.py` defines the station, market series, official CLI
product, coordinates, and both timezones. `settlement_calendar.py` supplies the
weather-side clock and `city_truth.py` maintains station-keyed final truth.

The shared path is `nwp_archive.py` → `emos_forecast.py` → a Gaussian mean and
spread in `forecast_emos_daily_high`. It uses eight configured NWP members,
per-station rolling-origin training, and final truth strictly before the target.
Scheduled archive maintenance fetches leads one and two; lead three is an
explicit historical-backfill/research capability. Live serving covers today
through today+2 using the corresponding lead.

`google_weather_cache.py` is the stable CLI/facade. With `--cities sfo`, its
orchestrator serves the all-city non-Google EMOS baseline before the budgeted
SFO provider refresh. The main refresh runs twice hourly from 05:10 through 18:40 PT and hourly overnight.
The separate non-SFO unit has a lower-frequency schedule.
Do not add a second EMOS command to the main refresh unit or replace its SFO
selection with all cities without measuring the provider budget.

`sfo-operational-publish.timer` publishes the operational JSONs and
`publication_manifest.json` every five minutes. The research-only
`sfo-strategy-lab-refresh.timer` follows a wall-clock five-minute cadence;
its slower analysis is separate from operational publication.

Google raw values remain in `google_weather_store.py`'s private, expiring runtime
store. A permanent usage ledger retains counts and budget reservations;
compatibility status JSON contains availability and usage, not new raw forecast
values. `google_runtime_blend.py` and `google_paired_evidence.py` support bounded
research challengers and paired evidence. Historical legacy blend tables are
not proof that the current orchestrator keeps permanently archiving raw Google
weather. Public artifacts undergo provider-field checks.

Apple WeatherKit stays outside serving, EMOS training and trading decisions.
Its temporary cache has provider-expiry and purge boundaries. Read
[APPLE-WEATHERKIT.md](../docs/APPLE-WEATHERKIT.md) before using it for research;
retrospective conditions are not original forecast vintages.

## Truth and calibration

- `nws_station_observations` and observation-derived highs provide intraday
  context. Completeness and same-day high-so-far do not prove final settlement.
- `cli_settlements` holds station/date truth. Only admissible final rows supply
  official settlement and exact forecast scoring; preliminary reports remain
  pending. `truth_store.py` provides the storage boundary.
- `emos_sources.py` selects the live-faithful `rolling_origin_v2` archive within
  station/lead scope; legacy `rolling_origin` is a fallback when v2 is absent.
  Do not mix versions or silently overwrite duplicate source rows.
- A high derived from hourly inputs requires every timestamp in the station's
  climate window. Partial response maxima are not complete highs. The live
  daily-max endpoint does not expose initialization or constituent-hour
  completeness; new live evidence preserves those fields as unavailable.
- `forecast_scoring.py`, `scores.py` and the post-processing backtests support
  proper scores. Compare CRPS and calibration as well as MAE; profitability
  additionally depends on decision-time price, fees and execution evidence.

## Canonical files

| Purpose | Source |
|---|---|
| Market/station registry | `cities.py` (intentional parity mirror in trading) |
| Fixed-lead NWP archive | `nwp_archive.py` |
| Original live member/vintage evidence | `live_forecast_evidence.py` |
| EMOS fit and serve | `emos_forecast.py`, `postproc_models.py` |
| Recalibration | `emos_recalibration.py`, `postproc_recalibration.py` |
| Final station truth | `city_truth.py`, `clisfo.py`, `truth_store.py` |
| Intraday observations | `nws_ground_truth.py` |
| Optional Google orchestration | `google_weather_cache.py`, `google_multicity_refresh.py` |
| Legacy SFO adapters | `blend_sources.py`, `blend_learners.py`, `blend_archive.py` |
| Offline SFO training | `research/` |
| Historical ML comparison | [retained September report](../docs/research/2026-09-05-ml-challenger.md), scoped to its original draft revision |

## Local commands

Install from the repository root, then use `forecaster/` as the working directory
for standalone forecast scripts:

```bash
.venv-dev/bin/python -m pip install -e '.[dev]'
cd forecaster
../.venv-dev/bin/python emos_forecast.py --serve-rolling --cities all
../.venv-dev/bin/python nwp_archive.py --help
../.venv-dev/bin/python city_truth.py --help
../.venv-dev/bin/python google_weather_cache.py --help
```

Serving and refresh commands make network requests and mutate local artifacts.
They require an appropriate local dataset. They do not synchronize AWS runtime
state. Do not substitute stale ignored `weather.db` for a production export.

Run heavy historical SFO preparation/training locally with `.[train]`:

```bash
cd forecaster
../.venv-dev/bin/python research/combine_psv.py --dir '2016-2026 weather data' --out combined_weather.csv
../.venv-dev/bin/python research/load_to_db.py
../.venv-dev/bin/python research/features.py
../.venv-dev/bin/python research/xgboost_model.py
../.venv-dev/bin/python research/lstm_model.py
../.venv-dev/bin/python research/compare_models.py
../.venv-dev/bin/python research/ab_test.py
```

These older SFO studies target a local calendar day and include a spot-temperature
sanity check. They are separate from the market's fixed-standard climate-day
pipeline. `ab_test_results.json`, `model_compare_results.json`, and public
`diagnostics.json` are retained historical research outputs; no production timer
retrains the LSTM or automatically promotes their numbers. Avoid comparing
unpaired MAE summaries as though they use the same eligible dates.

Run tests from the root with `bash scripts/run_tests.sh`. Runtime scheduling and
publishing belong to `trading/deploy/aws/`; see the
[repository map](../docs/REPOSITORY_MAP.md) and
[deployment runbook](../docs/aws_deployment.md).
