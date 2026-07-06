# WeatherEdge Context

WeatherEdge is a station-aligned SFO weather forecasting and Kalshi
paper-trading research project.

## Domain Terms

- **SFO station high**: the daily high temperature at KSFO/SFO, not a generic
  San Francisco city-center forecast.
- **Forecast blend**: weighted Google Weather, NWS, Open-Meteo, SFO history,
  and capped live-station adjustment.
- **Ground truth**: final KSFO high from NWS station observations or CLISFO-like
  settlement sources.
- **Kalshi bin**: a mutually exclusive settlement bucket for the SFO high
  temperature market.
- **Observed-high lock**: same-day rule that prevents impossible lower bins once
  KSFO has already observed a higher temperature.
- **Boundary-aware intraday math**: probability adjustment near settlement bin
  edges, especially before the afternoon high window.
- **Paper trade**: simulated trade recorded against real Kalshi market prices.
  It does not place a real order.
- **Target exposure cap**: cumulative per-target-date paper risk limit as a
  fraction of bankroll; there is no daily spend budget.
- **Balanced profile**: paper-research risk profile; statistically conservative
  (lower-bound edge must be non-negative) with structural liquidity gates.

## Architecture Terms

- **Forecaster module**: `forecaster/`, the station-aligned weather pipeline,
  Google/NWS/Open-Meteo blend, SQLite forecast archive, and NWP/EMOS
  post-processing.
- **Trading module**: `trading/sfo_kalshi_quant/`, the Kalshi market adapter,
  probability engine, risk gates, and paper-trading journal.
- **Deployment module**: `trading/deploy/aws/`, the scripts and systemd units
  that preserve the current AWS split-folder runtime.
- **Web app**: the React + HeroUI Pro SPA at the repo root (`src/`), built with
  bun and published to GitHub Pages from `/opt/weatheredge/webdist`.
- **Data artifacts**: runtime JSONs generated in `forecaster/` on the box and
  overlaid onto the published site every cycle: `trading_signal.json`,
  `strategy_research.json`, `forecast_data.json`, `weather_story_data.json`
  (plus `google_weather_cache.json` kept server-side).
