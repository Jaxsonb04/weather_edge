# Lightsail And Local Dataset Plan

> **Historical plan (2026-06-12).** This document records the former 1 GB
> Lightsail-era dataset constraints. Production migrated to EC2 on 2026-07-10.
> See [AWS Deployment](aws_deployment.md) and the
> [current codebase-audit brief](prompts/codebase-audit-fable5.md) for active
> operations and audit scope.

This project should collect derived SFO forecast/trading features, not bulk raw
weather archives. The Lightsail runtime is good for scheduled API pulls,
SQLite writes, and small point extractions. The local Mac can do heavier
research extraction when Lightsail is too small, but the deployable artifact
should still be compact derived features.

## Runtime Rule

Keep raw downloaded files temporary. Persist only:

- source name and version
- issued/run time
- target local date
- valid time or lead hour
- SFO station/grid metadata
- extracted features
- source URL or object key
- fetch timestamp
- quality/status flags

This keeps backtests point-in-time and keeps `/opt/weatheredge` small enough for
the existing Lightsail deployment.

## Machine Roles

Use two roles instead of forcing every dataset through the production box.

| Machine | Good for | Avoid |
| --- | --- | --- |
| Lightsail | Scheduled live refresh, Kalshi scans, small historical API pulls, SQLite joins, dashboard publishing | Full raw model archives, heavy GRIB/Zarr scans, long training jobs |
| Local Mac | Backfills, raw HRRR/NBM/NDFD/GFS experiments, dependency-heavy decoding, walk-forward research, model training | Treating local ignored artifacts as production truth |

Local output can be staged under ignored paths such as `trading/data/`,
`forecaster/weather.db`, or `/private/tmp/weatheredge_feature_store/`. After a
local extraction is validated, copy only compact derived tables to Lightsail.

## Tier 1: Run Directly On Lightsail

These are deployable on the current scheduled box. They are small enough for
systemd timers and SQLite.

| Dataset | Use | Runtime shape |
| --- | --- | --- |
| NOAA Global Hourly / ISD for KSFO and nearby stations | Official observed hourly weather, daily highs, station context | Yearly CSV/API chunks; persist hourly rows and daily aggregates |
| NWS API observations and grid forecast | Current official observation and live NWS forecast input | Existing refresh path; archive snapshots |
| NWS CLISFO / climate report settlement | Official-style SFO daily settlement high | Fetch latest report and recent versions; persist report date, max temp, issue time |
| Iowa Mesonet ASOS archive | Fast gap-fill for SFO/OAK/SJC/SQL/PAO/HAF observations | CSV pulls by date range; use as fallback/reconciliation |
| Open-Meteo Historical Forecast and Previous Runs | Practical historical HRRR/GFS/NBM/ECMWF/GraphCast-style forecast features | JSON/CSV point pulls; persist lead-time forecast rows only |
| Open-Meteo live ensemble | Forecast distribution/spread for bucket probabilities | Existing small JSON pull; archive member summary |
| Kalshi historical markets | Market metadata, listed bins, settlement values | Paginated JSON; persist event/market metadata nightly |
| Kalshi historical candles/trades | Point-in-time market prices for backtests | Opt-in narrow pulls on Lightsail; broader backfills run locally to avoid API rate limits |
| NOAA CO-OPS San Francisco station | Bay air temp, water temp, pressure, wind | Hourly API pulls; persist auxiliary features |
| NDBC nearby buoy stations | Marine layer/ocean context | Yearly text files; persist daily/hourly features only |

## Tier 2: Possible On Lightsail, But Use Sparingly

These can work only if each job extracts a tiny SFO point/region and deletes raw
files immediately. They may require additional system packages such as `wgrib2`,
`degrib`, or GRIB Python bindings, so they should not be added to the live
refresh loop until measured.

| Dataset | Use | Constraint |
| --- | --- | --- |
| NDFD gridded forecasts | Official NWS digital forecast archive/current max temp | Feasible for selected elements like max temp, temp, dewpoint, wind; avoid broad file scans |
| NBM raw GRIB/COG | Calibrated model blend benchmark | Prefer Open-Meteo NBM first; raw NBM only for limited point extraction |
| HRRR Zarr point extraction | High-resolution short-range SFO microclimate signal | Possible with xarray/zarr, but dependencies and memory are risky on 1 GB Lightsail |
| RAP/NAM point extraction | Alternate short-range model inputs | Lower priority than HRRR/NBM; only add if measured lift exists |

## Tier 3: Local/Offline Feature Store Only

These are beneficial for research but not deployable as raw processing on the
current Lightsail machine. Use the local Mac first. If local extraction becomes
too slow or too large, move the same scripts to a larger temporary EC2 instance,
AWS Batch, or Lambda/S3 pipeline. Ship only compressed derived tables back to
Lightsail.

| Dataset | Why not live on Lightsail | Acceptable output |
| --- | --- | --- |
| Full HRRR GRIB archive | Multi-GB/day patterns, many files, GRIB tooling | SFO point forecasts by run/lead plus neighborhood summary |
| Full GFS/GEFS archive | Global fields and ensemble members are large | Daily SFO point/member summaries |
| Full NBM archive | Large CONUS files and many lead hours | SFO point forecast table, uncertainty/percentile features |
| Full NDFD history | Product layout and decoder complexity | Daily SFO max/min/temp/wind point features |
| GOES satellite | Huge imagery, low direct value for daily high | Optional cloud/fog summary if extracted elsewhere |
| NEXRAD/MRMS radar | Precip/radar fields are not central to SFO daily high | Optional marine-layer/precip flags, not raw radar |

## Recommended Ingestion Order

1. Backfill official observations and settlement truth:
   NOAA Global Hourly, NWS ground truth, CLISFO, and Iowa Mesonet fallback.
2. Backfill market history:
   Kalshi `KXHIGHTSFO` historical markets on Lightsail, then 1-hour candles and
   trades through local or narrow one-off jobs.
3. Backfill practical historical forecasts:
   Open-Meteo Previous Runs / Historical Forecast for HRRR, GFS, NBM, ECMWF IFS,
   AIFS, GraphCast, and ensemble mean where available.
4. Add lightweight marine/bay context:
   NOAA CO-OPS and NDBC.
5. Only then test raw NDFD/NBM/HRRR point extraction behind a feature flag.

## Source Catalog

Use these public source locations as the acquisition map. A dataset being in an
AWS public bucket means access is available from AWS infrastructure; it does not
mean the current Lightsail instance should process the raw files inline.

| Source | Public access | WeatherEdge role |
| --- | --- | --- |
| NOAA Global Hourly / ISD | `https://www.ncei.noaa.gov/data/global-hourly/access/{year}/{station}.csv` | Official hourly observations for station truth and daily high features |
| Iowa Mesonet ASOS | `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py` | Fast ASOS fallback and reconciliation source |
| Open-Meteo Previous Runs | `https://previous-runs-api.open-meteo.com/v1/forecast` | Point forecast features with 1-7 day lead-time offsets |
| Open-Meteo Historical Forecast | `https://historical-forecast-api.open-meteo.com/v1/forecast` | Lightweight benchmark forecast archive |
| Kalshi historical markets/candles/trades | `https://external-api.kalshi.com/trade-api/v2` | Market metadata, prices, volume, liquidity, and fills proxy |
| HRRR on AWS | `s3://noaa-hrrr-bdp-pds/`, `s3://hrrrzarr/` | Local or larger-instance point extraction before compact feature export |
| NBM on AWS | `s3://noaa-nbm-pds/`, `s3://noaa-nbm-grib2-pds/` | Local or measured Lightsail point extraction only |
| NDFD on AWS | `s3://noaa-ndfd-pds/` | Official gridded max temp benchmark after decoder proof |
| GFS on AWS | `s3://noaa-gfs-bdp-pds/` | Lower-resolution global model baseline features |

## Known Source Issues

- NOAA Global Hourly / ISD: On June 10, 2026, the NCEI Global Hourly endpoint
  stalled during local smoke tests even after switching the reader to stream
  and stop at the requested date window. Keep NOAA ISD in the plan, but do not
  block the first deployed dataset pass on it. Use Iowa Mesonet ASOS as the
  observation fallback/reconciliation source, then retry NOAA ISD as a scheduled
  backfill when NCEI access is stable.

## Hybrid Workflow

1. Prototype locally with a tiny date range, usually 3-7 completed SFO market
   days.
2. Extract one SFO point plus a small neighborhood summary, not whole CONUS
   fields.
3. Write a manifest recording source URLs/S3 keys, run times, target dates, and
   extraction code version.
4. Run walk-forward checks locally against Kalshi candles/trades and settlement
   truth.
5. Promote only the derived feature table and manifest to Lightsail.
6. Add the source to the live blend only after it improves out-of-sample skill
   or trading calibration after costs.

## Commands

Prototype a one-day deployable backfill locally:

```bash
cd trading
SFO_DATASET_DB=/private/tmp/weatheredge_dataset_probe.db \
SFO_DATASET_START_DATE=2026-06-02 \
SFO_DATASET_END_DATE=2026-06-02 \
SFO_TRADING_ROOT="$(pwd)" \
SFO_TRADING_PYTHON=python3 \
bash deploy/aws/run_dataset_backfill.sh
python3 -m sfo_kalshi_quant.cli --db-path /private/tmp/weatheredge_dataset_probe.db \
  dataset-status
```

Run the Lightsail-safe backfill into the normal paper DB:

```bash
cd /opt/weatheredge/trading
bash deploy/aws/run_dataset_backfill.sh
```

Fetch Kalshi hourly candles and trades for a narrow historical window. Prefer
running this locally for broad backfills because Kalshi can rate-limit the
request-heavy detail endpoints:

```bash
python3 -m sfo_kalshi_quant.cli \
  dataset-backfill --source kalshi-history --start-date 2026-04-01 \
  --end-date 2026-06-10 --kalshi-candles --kalshi-trades \
  --candle-interval 60 --kalshi-max-trade-pages 3
```

Use local-only heavy extraction for raw HRRR/NBM/NDFD later, but write the same
kind of rows into `dataset_forecast_features`: source, model, issued time,
target date, valid time, lead hours, variable, value, units, and source key.

## Storage Budget

Target the following on Lightsail:

- `weather.db`: below 2 GB
- `paper_trading.db`: below 1 GB
- temporary raw downloads: below 1 GB and cleaned per job
- derived feature exports: compressed CSV/Parquet/SQLite, not raw GRIB/Zarr

If a source cannot fit this shape, it belongs in an offline feature extraction
pipeline, not in the live dashboard/paper-trading runtime.

Local storage can be larger, but raw archives should still be treated as
rebuildable cache. Do not commit raw HRRR/NBM/NDFD/GFS files, and do not let
local ignored runtime files override AWS-generated dashboard state.

## Backtest Guardrails

- Use issued/run time, not target date, when joining forecasts to markets.
- Never use a forecast run that was unavailable at the simulated decision time.
- Keep recent holdout data untouched.
- Compare added sources with walk-forward splits before giving them live weight.
- Track transaction costs, spread, and liquidity separately from forecast MAE.
