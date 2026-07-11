# WeatherEdge

WeatherEdge is a unified weather forecasting and prediction-market paper-trading research
project covering fifteen US city daily-high markets, with SFO as the flagship.
It combines a station-aligned SFO forecaster with its Google/NWS/Open-Meteo
blend, a station-agnostic NWP→EMOS pipeline for the other fourteen cities, a
prediction-market probability engine, paper-trading journal, AWS deployment
scripts, and a React single-page dashboard published to GitHub Pages.

**Safety rule:** this project is paper trading only. It uses real Kalshi market
prices for research, but it does not place live real-money orders.

## Cities

The city registry is `forecaster/cities.py`, duplicated byte-identically as
`trading/sfo_kalshi_quant/cities.py` (a parity test enforces this). Each entry
defines the slug, name, Kalshi series ticker, NWS settlement station, CLI
product (site + issuedby), lat/lon, civil timezone, and fixed standard-time UTC
offset. Every market settles on its own NWS Climatological Report (CLI), and
each city's climate day runs midnight-to-midnight in local standard time.

Forecasting is two-tier:

- **SFO** keeps the full legacy blend: Google Weather (budgeted), LSTM,
  marine-layer features, plus the NWP/EMOS archive.
- **All other cities** run the station-agnostic NWP→EMOS→CLI path only:
  Open-Meteo previous-runs archive (9 models, leads 1-3), rolling-origin EMOS
  per city, and settlement truth in the station-keyed `cli_settlements` table
  fed by live CLI scans plus the IEM archive backfill
  (`forecaster/city_truth.py`).

## What Is Here

```text
WeatherEdge/
  forecaster/   weather pipeline: SFO blend, multi-city NWP/EMOS archive,
                cities.py registry, CLI settlement truth
  trading/      Kalshi probability, risk gates, CLI, paper journal, AWS scripts
  src/          React SPA (the public site), built with bun + Vite
  docs/         unified guides, glossary, sync/deploy notes
  pyproject.toml
  CONTEXT.md
```

## Quick Start

```bash
cd /path/to/WeatherEdge
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python trading/tests/run_tests.py
```

Without installing first, use the helper:

```bash
bash scripts/run_tests.sh
```

Before syncing, pushing, or deploying, run the full local verification gate:

```bash
bash scripts/verify_project.sh
```

It runs the WeatherEdge health check, trading tests, and Python compile check.
Warnings about Git not being initialized or Semgrep not being installed are
informational until you decide to turn those on.

Analyze today and tomorrow with paper-trading gates. The loop covers all
fifteen registered cities by default (env `PAPER_CITIES`, default `all`); pass
`--cities` with a comma list of slugs to narrow it:

```bash
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both --side both
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both --side both --cities sfo,lax
```

Without installing first:

```bash
bash scripts/paper_analyze.sh
```

Paper analysis defaults to the `live` paper-research profile (the stricter,
real-trading-candidate book, paper-only until a readiness gate passes). Use
`--risk-profile research` when you want the loosest paper-only gates at the
smallest size so the journal fills faster with the full opportunity set:

```bash
python -m sfo_kalshi_quant.cli --no-color --risk-profile live analyze --target-date both
python -m sfo_kalshi_quant.cli --no-color --risk-profile research analyze --target-date rolling --side both --place-paper --paper-stake 5
```

To run live and research side by side in one paper DB, set:

```bash
PAPER_RISK_PROFILES=live,research bash scripts/paper_analyze.sh --target-date rolling --place-paper
```

Record paper trades only when the CLI says `TRADE`:

```bash
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both --side both --paper-stake 10 --place-paper
```

## Forecast Workflow

Run forecaster commands from `forecaster/` because the legacy scripts use
project-relative paths:

```bash
cd /path/to/WeatherEdge/forecaster
python combine_psv.py --dir "2016-2026 weather data" --out combined_weather.csv
python load_to_db.py
python features.py
python forecast_tomorrow.py
python nws_ground_truth.py --days 14
python google_weather_cache.py
```

Refreshing Google Weather requires `GOOGLE_WEATHER_API_KEY`. The project keeps
Google usage disciplined with an 8,000/month and 260/day default event budget,
below the 10,000 free monthly cap.

These commands drive the SFO legacy blend. The other fourteen cities run
through the NWP→EMOS path (`nwp_archive.py`, `emos_forecast.py`) with CLI
settlement truth from `city_truth.py`; the AWS timers run these with
`--cities all`.

## Public Website (React SPA)

The public site is a React + Vite + HeroUI Pro single-page app at the repo root
(`src/`, `index.html`, `vite.config.ts`), built with bun:

```bash
bun install --frozen-lockfile # HeroUI Pro registry auth required (HEROUI_PERSONAL_TOKEN)
bun run build # outputs dist/
```

Production serves the prebuilt app from `/opt/weatheredge/webdist` on the EC2
box; `trading/deploy/aws/publish_forecaster_pages.sh` publishes it to
the `gh-pages` branch with the freshly generated data JSONs
(`trading_signal.json`, `forecast_data.json`, `weather_story_data.json`,
`strategy_research.json`, `cities_data.json`) overlaid on every refresh cycle.
The site includes a fifteen-city Coverage grid fed by `cities_data.json`
(per-city forecasts, latest settlement, book activity), with SFO presented as
the flagship. To ship a new app build, copy `dist/` to
`/opt/weatheredge/webdist` and run one strategy-lab refresh.

## Kalshi Workflow

Run trading commands from the repository root after installing with
`pip install -e .`, or from `trading/` with its local package layout.

Important commands:

```bash
python -m sfo_kalshi_quant.cli backtest-calibration
python -m sfo_kalshi_quant.cli backtest-calibration --source clean-blend
python -m sfo_kalshi_quant.cli daily-report --target-date both --side both --format json --no-live-market --output forecaster/trading_signal.json
python -m sfo_kalshi_quant.cli strategy-research --output forecaster/strategy_research.json
python -m sfo_kalshi_quant.cli analyze --target-date both --side both
python -m sfo_kalshi_quant.cli analyze --target-date both --side both --cities sfo,lax
python -m sfo_kalshi_quant.cli backtest-signals
python -m sfo_kalshi_quant.cli paper-report
python -m sfo_kalshi_quant.cli paper-monitor
python -m sfo_kalshi_quant.cli paper-settle --target-date YYYY-MM-DD --settlement-high 67
```

`daily-report` is read-only dashboard input; it does not record DB snapshots or
place paper orders.

Strategy Lab defaults to the `live` profile view, so the wider-net
`research` results do not contaminate the `live` headline P&L, hit rate,
open risk, daily rows, signals, actions, or learnings. The AWS
`sfo-strategy-lab-refresh.timer` republishes those trading results every fifteen
minutes without calling the paid Google Weather refresh path.

`backtest-calibration --source clean-blend` validates the archived live blend on
clean next-day forecasts only. It excludes same-day observed-high lock/floor
rows.

`--settlement-high 67` means the official resolved SFO high was 67°F for that
date. Exposure caps and settlement are series-scoped, so one city's high can
never settle another city's bins; automatic settlement walks each city's own
NWS CLI product, with archived CLI truth as fallback.

## Repository Sync

Configure a Git remote and review ignored files before publishing changes:

```bash
git status
git status --ignored
```

Optional deployment scripts can sync the app into a server layout such as:

```text
/opt/weatheredge/forecaster
/opt/weatheredge/trading
```

See [docs/aws_deployment.md](docs/aws_deployment.md).

## Data And Artifacts

Local WeatherEdge may include copied raw KSFO NOAA station files and ignored
runtime artifacts from previous runs. After AWS sync and refresh, live
DB/cache/dashboard state is authoritative on AWS, not on this MacBook. Clear
stale local runtime state before dashboard design smoke tests:

```bash
python3 scripts/clear_local_runtime_state.py --confirm
```

The root `.gitignore` prevents large raw data and live runtime DB/cache files
from being committed accidentally.

See [docs/data_and_artifacts.md](docs/data_and_artifacts.md).

## Learning Path

Start with:

1. [docs/glossary.md](docs/glossary.md)
2. [trading/docs/user_guide.md](trading/docs/user_guide.md)
3. [docs/architecture.md](docs/architecture.md)
4. [docs/operational_runbook.md](docs/operational_runbook.md)
5. [docs/research_improvement_review.md](docs/research_improvement_review.md)

The math should stay auditable: probability, calibration, risk gates, observed
high locks, and paper PnL should be explainable from code and docs.
