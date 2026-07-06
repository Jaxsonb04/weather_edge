# Glossary

## Weather

- **KSFO/SFO**: the airport weather station and settlement target. This project
  predicts SFO Airport, not all of San Francisco.
- **Daily high**: maximum observed temperature for the NWS/Kalshi report date.
  During daylight saving time this follows Pacific standard time, not the
  midnight-to-midnight daylight clock.
- **Forecast blend**: weighted combination of Google Weather, NWS, Open-Meteo,
  SFO history, and a capped live-airport adjustment.
- **Source spread**: disagreement between forecast sources. Larger spread means
  more uncertainty.
- **Ground truth**: resolved observed high from NWS KSFO observations or an
  official settlement source, aligned to the NWS/Kalshi report date.
- **Google event budget**: local limit that prevents Weather API usage from
  accidentally exceeding the intended free-tier budget.

## Kalshi

- **Event**: one daily SFO high-temperature question on Kalshi.
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
- **Paper trade**: simulated trade recorded locally. No live order is placed.

## Project

- **Forecaster**: `forecaster/`, the weather data, blend, archive, and site
  data generator.
- **Trading engine**: `trading/sfo_kalshi_quant/`, the Kalshi analyzer and
  paper-trading CLI.
- **Dashboard**: the React + Vite SPA served from GitHub Pages. The publisher
  ships the prebuilt app from `/opt/weatheredge/webdist` plus fresh data JSONs
  to `gh-pages` every refresh cycle.
