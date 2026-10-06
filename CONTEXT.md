# WeatherEdge Context

WeatherEdge is a station-aligned weather forecasting and Kalshi paper-trading
research project covering twenty US city daily-high markets, with SFO as the
flagship.

For canonical current source and runtime boundaries, read
[the repository map](docs/REPOSITORY_MAP.md) and
[architecture](docs/architecture.md). Production coverage and policy require
artifact provenance; they need not match the current source registry.

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
- **Forecast blend**: optional legacy SFO weighted-source path. The shared
  baseline is station-specific NWP→EMOS→CLI, including SFO when its artifact
  names EMOS. Raw optional provider availability is not a promoted model.
- **Ground truth**: admissible final station CLI high stored station/date-keyed
  in `cli_settlements`. Observation-derived high-so-far is nonfinal context.
- **Kalshi bin**: a mutually exclusive settlement bucket for a city's daily
  high temperature market.
- **Observed-high lock**: same-day rule that prevents impossible lower bins once
  KSFO has already observed a higher temperature.
- **Boundary-aware intraday math**: probability adjustment near settlement bin
  edges, especially before the afternoon high window.
- **Paper trade**: simulated trade recorded against real Kalshi market prices.
  It does not place a real order.
- **Target exposure cap**: cumulative per-target-date paper risk limit,
  series-scoped per city, alongside account cash, concentration and daily-loss
  capacity checks. Requested size is not guaranteed filled exposure.
- **Live Stability / Research ROI**: separate economic paper accounts. Live
  Stability alone contributes to readiness; versioned Research ROI is bounded
  paper research. Legacy CLI aliases do not create extra active accounts.
- **Reservation-first entry**: production entry mode (`PAPER_ENTRY_MODE=limit`)
  normally rests limits at the reservation price. Profile-scoped guarded
  taker crosses may instead capture displayed whole-contract depth when exact
  after-fee point and lower-bound edge still pass; otherwise the order remains
  maker. Resting fills use a tape-based proxy with no queue-position simulation.
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
  twenty cities with per-city forecaster adapters and settlement clocks.
- **Deployment module**: `trading/deploy/aws/`, the scripts and systemd units
  that preserve the current AWS split-folder runtime.
- **Web app**: the React + HeroUI Pro SPA at the repo root (`src/`), built with
  bun and published to GitHub Pages from `/opt/weatheredge/webdist`.
- **Data artifacts**: runtime JSONs generated in `forecaster/` on the box and
  overlaid onto the published site every cycle: `trading_signal.json`,
  `strategy_research.json`, `forecast_data.json`, `weather_story_data.json`,
  and `cities_data.json` (per-city forecasts, latest settlement, book
  activity), plus `google_weather_cache.json` kept server-side.
