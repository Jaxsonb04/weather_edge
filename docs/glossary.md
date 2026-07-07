# Glossary

## Weather

- **City registry**: `forecaster/cities.py` (duplicated byte-identically as
  `trading/sfo_kalshi_quant/cities.py`, parity-tested), defining each of the
  fifteen city markets: slug, name, Kalshi series ticker, NWS settlement
  station, CLI product (site + issuedby), lat/lon, civil timezone, and fixed
  standard-time UTC offset.
- **KSFO/SFO**: the airport weather station and flagship settlement target.
  This project predicts SFO Airport, not all of San Francisco. Every market
  is station-specific the same way (e.g. Chicago is Midway/KMDW, Houston is
  Hobby/KHOU, NYC is Central Park/KNYC).
- **Daily high**: maximum observed temperature for the NWS/Kalshi report date.
  Each city's climate day is midnight-to-midnight in local standard time, so
  during daylight saving time it does not follow the daylight clock.
- **Forecast blend**: weighted combination of Google Weather, NWS, Open-Meteo,
  SFO history, and a capped live-airport adjustment. SFO-only; other cities
  use per-city EMOS forecasts from the NWP archive.
- **CLI settlements table**: station-keyed `cli_settlements` table in
  `weather.db` holding settlement truth from each city's NWS Climatological
  Report (CLI), fed by live CLI scans plus the IEM archive backfill.
- **Source spread**: disagreement between forecast sources. Larger spread means
  more uncertainty.
- **Ground truth**: resolved observed high from NWS KSFO observations or an
  official settlement source, aligned to the NWS/Kalshi report date.
- **Google event budget**: local limit that prevents Weather API usage from
  accidentally exceeding the intended free-tier budget.

## Kalshi

- **Event**: one daily city high-temperature question on Kalshi. Each city has
  its own series ticker (from the city registry), and one city's high can
  never settle another city's bins.
- **Market/bin**: one mutually exclusive temperature range inside the event.
- **YES side**: pays if the bin is the official settlement bin.
- **NO side**: pays if the bin is not the official settlement bin.
- **Bid/ask**: current market buy/sell prices. The model uses real orderbook
  prices for paper trading.
- **Edge**: expected value after estimated fee and entry price.
- **Lower-confidence probability**: conservative probability used to avoid
  trading fragile edges.
- **Observed-high lock**: same-day rule that rules out lower bins once SFO has
  already hit a higher temperature.
- **Clean next-day forecast**: archived blend snapshot made on the SFO day
  before the target date, with no same-day observed-high lock/floor.
- **Cheap-tail gate**: special guardrail for 1c/2c contracts with weak bid
  support.
- **Maker-first entry**: the production entry mode (`PAPER_ENTRY_MODE=limit`)
  that rests limit orders instead of crossing the spread. Resting quotes pay
  the maker fee (25% of the 0.07 quadratic taker rate); the monitor fills a
  resting limit when the visible ask crosses it — a proxy fill model with no
  queue-position simulation.
- **Favorite band**: the live profile's entry price gate [0.70, 0.97],
  concentrating on high-probability favorites per the favorite-longshot-bias
  evidence. The research profile still trades the whole price curve.
- **Paper trade**: simulated trade recorded locally. No live order is placed.

## Project

- **Forecaster**: `forecaster/`, the weather data, SFO blend, multi-city
  NWP/EMOS archive, CLI settlement truth, and site data generator.
- **Trading engine**: `trading/sfo_kalshi_quant/`, the Kalshi analyzer and
  paper-trading CLI, looping all fifteen registered cities.
- **Dashboard**: the React + Vite SPA served from GitHub Pages. The publisher
  ships the prebuilt app from `/opt/weatheredge/webdist` plus fresh data JSONs
  (including `cities_data.json` for the fifteen-city Coverage grid) to
  `gh-pages` every refresh cycle.
