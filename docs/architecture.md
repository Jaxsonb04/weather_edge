# Architecture

WeatherEdge deliberately keeps two deep modules with a small interface between
them. The system covers fifteen US city daily-high markets; SFO is the
flagship.

## City Registry

The registry of markets lives in `forecaster/cities.py` and is duplicated
byte-identically as `trading/sfo_kalshi_quant/cities.py` (a parity test
enforces this). Each entry defines the slug, name, Kalshi series ticker, NWS
settlement station, CLI product (site + issuedby), lat/lon, civil timezone,
and fixed standard-time UTC offset. Every market settles on its own NWS
Climatological Report (CLI); each city's climate day is midnight-to-midnight
in local standard time.

## Forecaster Module

Path: `forecaster/`

Responsibilities:

- ingest KSFO station history
- archive NWS observations and daily highs
- fetch/cache Google Weather within the event budget (SFO only)
- blend Google, NWS, Open-Meteo, and SFO history (SFO only)
- run the station-agnostic NWP→EMOS→CLI path for the other fourteen cities:
  Open-Meteo previous-runs archive (9 models, leads 1-3) and rolling-origin
  EMOS per city
- maintain CLI settlement truth in the station-keyed `cli_settlements` table,
  fed by live CLI scans plus the IEM archive backfill
  (`forecaster/city_truth.py`)
- generate the site data JSONs consumed by the public SPA
- keep forecast archive tables in `weather.db`; `nwp_model_forecasts` and
  `forecast_emos_daily_high` are station-keyed (auto-migration)
- score forecast skill only on clean next-day snapshots; same-day observed-high
  rows are settlement context

Main interface consumed by trading:

- `weather.db`
- `google_weather_cache.json`
- `ab_test_results.json`

## Trading Module

Path: `trading/sfo_kalshi_quant/`

Responsibilities:

- loop all registered cities in `analyze`/`portfolio-scan` (`--cities`, env
  `PAPER_CITIES=all`)
- read forecaster snapshots through a per-city forecaster adapter (SFO uses
  the full blend; other cities use the EMOS Gaussian snapshot, with the scored
  EMOS archive supplying calibration outcomes)
- fetch Kalshi public market/orderbook data
- convert forecast distributions into Kalshi bin probabilities
- apply same-day observed-high and boundary-aware intraday updates, with
  per-city settlement clocks
- evaluate YES/NO sides after fees, spread, confidence, and liquidity gates;
  the live profile additionally gates entries to the favorite band
  [0.70, 0.97], while research trades the whole price curve
- enter maker-first: production entry mode rests limit orders that pay the
  maker fee (25% of the quadratic taker rate); the monitor fills a resting
  limit when the visible ask crosses (a proxy fill model, no queue position)
- keep exposure caps and settlement series-scoped, so one city's high can
  never settle another city's bins; auto-settle walks each city's own CLI
  product with archived CLI truth as fallback
- record and monitor paper-only trades
- run walk-forward calibration on either LSTM held-out outcomes or clean
  archived blend outcomes (SFO); warm/hot cohort blocks and GFS-ensemble
  sharpening remain SFO-only

Main public interface:

```bash
python -m sfo_kalshi_quant.cli ...
```

## Deployment Module

Path: `trading/deploy/aws/`

Optional deployment scripts support a split server runtime:

```text
/opt/weatheredge/forecaster
/opt/weatheredge/trading
```

The sync script copies `forecaster/` and `trading/` into configurable remote
paths.

## Public Site

The public site is a React + Vite + HeroUI Pro SPA whose source lives at the
repository root (`src/`, `index.html`, `vite.config.ts`) and is built with
`bun run build`. The prebuilt app lives at `/opt/weatheredge/webdist` on the
server. Each refresh cycle, `trading/deploy/aws/publish_forecaster_pages.sh`
publishes `webdist` plus fresh `trading_signal.json`, `forecast_data.json`,
`weather_story_data.json`, `strategy_research.json`, and `cities_data.json`
(per-city forecasts, latest settlement, book activity) to the `gh-pages`
branch, which GitHub Pages serves at
`https://jaxsonb04.github.io/weather_edge/`. The site includes a fifteen-city
Coverage grid; SFO is presented as the flagship.

## Deepening Opportunities

Near-term architecture improvements:

- Wrap forecaster project-relative paths in a small config module.
- Move site data building into named functions with testable interfaces.
- Add a formal forecast snapshot schema shared by forecaster and trading.
- Add a stable market snapshot storage module before claiming real PnL edge.
- Keep paper trading and any future live-order path as separate modules.
