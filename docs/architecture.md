# Architecture

WeatherEdge deliberately keeps two deep modules with a small interface between
them.

## Forecaster Module

Path: `forecaster/`

Responsibilities:

- ingest KSFO station history
- archive NWS observations and daily highs
- fetch/cache Google Weather within the event budget
- blend Google, NWS, Open-Meteo, and SFO history
- generate the site data JSONs consumed by the public SPA
- keep forecast archive tables in `weather.db`
- score forecast skill only on clean next-day snapshots; same-day observed-high
  rows are settlement context

Main interface consumed by trading:

- `weather.db`
- `google_weather_cache.json`
- `ab_test_results.json`

## Trading Module

Path: `trading/sfo_kalshi_quant/`

Responsibilities:

- read forecaster snapshots through `SfoForecasterAdapter`
- fetch Kalshi public market/orderbook data
- convert forecast distributions into Kalshi bin probabilities
- apply same-day observed-high and boundary-aware intraday updates
- evaluate YES/NO sides after fees, spread, confidence, and liquidity gates
- record and monitor paper-only trades
- run walk-forward calibration on either LSTM held-out outcomes or clean
  archived blend outcomes

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
`weather_story_data.json`, and `strategy_research.json` to the `gh-pages`
branch, which GitHub Pages serves at
`https://jaxsonb04.github.io/weather_edge/`.

## Deepening Opportunities

Near-term architecture improvements:

- Wrap forecaster project-relative paths in a small config module.
- Move site data building into named functions with testable interfaces.
- Add a formal forecast snapshot schema shared by forecaster and trading.
- Add a stable market snapshot storage module before claiming real PnL edge.
- Keep paper trading and any future live-order path as separate modules.
