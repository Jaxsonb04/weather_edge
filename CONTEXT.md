# WeatherEdge Context

WeatherEdge is a station-aligned weather forecasting and Kalshi paper-trading
research project covering fifteen US city daily-high markets, with SFO as the
flagship.

## Domain Terms

- **City registry**: `forecaster/cities.py` (duplicated byte-identically as
  `trading/sfo_kalshi_quant/cities.py`, parity-tested) defining each market's
  slug, name, Kalshi series ticker, NWS settlement station, CLI product,
  lat/lon, civil timezone, and fixed standard-time UTC offset.
- **SFO station high**: the daily high temperature at KSFO/SFO, not a generic
  San Francisco city-center forecast. Every other city has the same
  station-specific meaning (e.g. Chicago settles at Midway/KMDW, Houston at
  Hobby/KHOU, NYC at Central Park/KNYC).
- **CLI settlement**: every market settles on its own NWS Climatological
  Report (CLI); each city's climate day is midnight-to-midnight in local
  standard time.
- **Forecast blend**: weighted Google Weather, NWS, Open-Meteo, SFO history,
  and capped live-station adjustment. SFO-only; non-SFO cities run the
  station-agnostic NWP→EMOS→CLI path.
- **Ground truth**: final station high from NWS observations or CLI settlement
  sources, stored station-keyed in the `cli_settlements` table.
- **Kalshi bin**: a mutually exclusive settlement bucket for a city's daily
  high temperature market.
- **Observed-high lock**: same-day rule that prevents impossible lower bins once
  KSFO has already observed a higher temperature.
- **Boundary-aware intraday math**: probability adjustment near settlement bin
  edges, especially before the afternoon high window.
- **Paper trade**: simulated trade recorded against real Kalshi market prices.
  It does not place a real order.
- **Target exposure cap**: cumulative per-target-date paper risk limit as a
  fraction of bankroll; there is no daily spend budget. Caps are series-scoped
  per city.
- **Balanced profile**: paper-research risk profile; statistically conservative
  (lower-bound edge must be non-negative) with structural liquidity gates.
- **Maker-first entry**: production entry mode (`PAPER_ENTRY_MODE=limit`) rests
  limit orders that pay the maker fee (25% of the 0.07 quadratic taker rate);
  the monitor fills a resting limit when the visible ask crosses it — a proxy
  fill model with no queue-position simulation.
- **Favorite band**: the live profile's price gate [0.70, 0.97], concentrating
  on high-probability favorites per the favorite-longshot-bias evidence; the
  research profile still trades the whole price curve.

## Architecture Terms

- **Forecaster module**: `forecaster/`, the station-aligned weather pipeline,
  SFO Google/NWS/Open-Meteo blend, SQLite forecast archive, city registry
  (`cities.py`), CLI settlement truth (`city_truth.py`), and per-city NWP/EMOS
  post-processing.
- **Trading module**: `trading/sfo_kalshi_quant/`, the Kalshi market adapter,
  probability engine, risk gates, and paper-trading journal, looping all
  fifteen cities with per-city forecaster adapters and settlement clocks.
- **Deployment module**: `trading/deploy/aws/`, the scripts and systemd units
  that preserve the current AWS split-folder runtime.
- **Web app**: the React + HeroUI Pro SPA at the repo root (`src/`), built with
  bun and published to GitHub Pages from `/opt/weatheredge/webdist`.
- **Data artifacts**: runtime JSONs generated in `forecaster/` on the box and
  overlaid onto the published site every cycle: `trading_signal.json`,
  `strategy_research.json`, `forecast_data.json`, `weather_story_data.json`,
  and `cities_data.json` (per-city forecasts, latest settlement, book
  activity), plus `google_weather_cache.json` kept server-side.
