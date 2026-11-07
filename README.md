# WeatherEdge

WeatherEdge forecasts station daily-high temperatures, converts distributions
into prediction-market bracket probabilities, and tests fee-aware trading
policies in separate paper accounts. It combines weather ingestion,
probabilistic calibration, orderbook evidence, simulated execution, official
settlement, AWS operations, and a React evidence dashboard.

[Live dashboard](https://jaxsonb04.github.io/weather_edge/) ·
[Strategy Lab](https://jaxsonb04.github.io/weather_edge/#/lab) ·
[Repository map](docs/REPOSITORY_MAP.md) ·
[Architecture](docs/architecture.md) ·
[Project case study](docs/PROJECT_CASE_STUDY.md) ·
[AI-assisted development](docs/ai-assisted-development.md)

[![Verify](https://github.com/Jaxsonb04/weather_edge/actions/workflows/verify.yml/badge.svg)](https://github.com/Jaxsonb04/weather_edge/actions/workflows/verify.yml)

[![WeatherEdge dashboard](docs/assets/dashboard.png)](https://jaxsonb04.github.io/weather_edge/)

**Paper trading only.** The engine reads public prediction-market prices and records
simulated orders. `live_execution.py` rejects non-dry orders; no authenticated
real-money execution client is implemented. Paper fills do not establish that
an equivalent real order would fill.

## Current evidence and paper accounts

Repository source covers twenty city markets. V7, M1 and the November 7 capital
changes were deployed on November 7, 2026: backend at `c14d1a10c`, web at
`372212b33`. The **October 11, 2026, 11:01 PDT backend snapshot** of `2a6432e3`
and fifteen markets is historical.
Repository source, installed backend, SPA assets, and generated runtime data
have separate provenance; a merged fix is not proof of a deployed fix.

| Area | Source capability and observed deployment |
|---|---|
| Coverage | 20 city markets, with SFO as the flagship |
| Policy | Live, V7 and M1 in separate $2,000 paper accounts; live execution disabled |
| Website | V7 history/audit and hybrid research UI; November 7 release verified the installed app, Pages deployment and public hashes; locked MapLibre 6.4.1 |
| Backend cutover | Done November 7: V7 and M1 active; V6 archived with monitoring and settlement preserved |

At the older **October 5, 19:52 PDT account snapshot**, the separate $1,000
initial-capital accounts reported:

| Paper account | Realized P&L | Return on initial capital |
|---|---:|---:|
| Live Stability | +$58.67 | +5.867% |
| Research ROI v6 | +$17.93 | +1.793% |

Research v6 earned $17.93 on $3,927.86 of resolved entry capital: **0.4564%
resolved-capital ROI**. Its approximately 72% profitable-exit rate did not imply
strong net profit. These accounts are economically separate; their profits and
balances must not be treated as one bankroll.

The [v7 audit](docs/research/2026-10-05-v7-audit.md) records the measured baseline,
release gap, thin fill capture, concentrated losses, stale analysis, and
reproduced forecast/exit evidence defects. Source corrections require verified
deployment and prospective validation. The **November 7, 2026 owner decision**
sets each active paper account's capital at $2,000: Live Stability, Research V7,
and the M1 trial. Live kept its ledger and order history with one audited
+$1,000 deposit; V7 and M1 got fresh ledgers. V7's reference equity is
$2,000, with no reserve. These accounts are never summed into one bankroll.
Archived V6 and older accounts keep their real $1,000 history and are never
restated. See the [paper limits and completed cutover](docs/aws_deployment.md#november-7-paper-capital-decision).
Strategy Lab shows the policy version in its artifact and all published archived
profiles, including zero-trade experiments.

The fresh [forecast baseline](docs/research/2026-10-05-v7-forecast-baseline.json)
records 125,170 NWP member rows and 10,642 source-separated scored cases. Its
1.80°F MAE and 1.30°F CRPS describe reconstructed archive skill; original
decision-time availability remains unestablished. The dated incident reports,
unassigned losses, and older ML challenger are retained alongside the audit.

The current objective is to gather meaningful evidence for or against
profitability. The $2,000 paper capital and V7's 5% ($100) daily goal are research
settings, not promised or achieved returns. Older targets remain dated research
assumptions. October 31, 2026 was the engineering and
evidence review date; readiness depends on the evidence. Increasing capital,
requested size, or activity does not create an edge or displayed liquidity. Scaling
requires account-scoped net P&L after fees, calibrated probabilities, realizable
execution, drawdown evidence, and chronological holdout validation. Live
allocation remains **$0 until the owner explicitly authorizes real money**.

The bounded Mac worker supports separate prospective weather research. As of
October 10, its store held 489 immutable forecast distributions; 139 unique
station/lead/date outcomes covered five completed dates, including day-ahead
diagnostics. This is not net trading profit or a promoted model. The five-city
reconstructed training-history bundle was deployed November 7. Two later
process-signaling failures remain under investigation. See the [October 11 consistency audit](docs/audits/2026-10-11-repo-consistency.md)
and [session memory](docs/SESSION_MEMORY.md) for the dated operational limits
and remaining recovery work.

## Forecasting and trading path

```text
NWP history + live vintages ► per-station EMOS ──► forecast distribution
Final station CLI truth ────► training and independent scoring        │
Fresh station observations ─► same-day feasible outcomes             ▼
Real orderbook/tape ────────► market posterior + exact fees + risk gates
                                                                  │
                                                                  ▼
                       separate paper accounts ──► AWS JSON ──► React SPA
```

The shared operational weather path uses eight NWP members and per-station
EMOS post-processing. Each city has a defined market series, settlement station,
NWS Climatological Report, and fixed-standard climate-day clock. SFO also has
legacy blend adapters, station history, LSTM/XGBoost research, and marine-layer
features. Those historical studies are not proof of current twenty-city serving
accuracy. An EMOS point must be paired with its matching EMOS distribution.

Google Weather runs through a budgeted, provider-expiring private runtime store.
The scheduled orchestrator serves the non-Google baseline first. Research
challengers and legacy SFO interfaces have explicit boundaries; optional source
availability does not establish a promoted forecast improvement. Apple
WeatherKit is a temporary private shadow source with zero live weight; see
[its boundary](docs/APPLE-WEATHERKIT.md). Neither provider's historical conditions
should be treated as an original decision-time forecast vintage.

Scheduled entry uses `portfolio-scan`: Live Stability and V7 reservation-price
maker limits and configured bounded taker crosses must pass exact fee, edge,
lower-bound, liquidity, concentration, loss, and account admission checks; M1
uses its frozen market-recalibration entry rule with separate risk checks.
Monitoring and settlement preserve each account's identity. Final settlement uses the city's
official final CLI truth; a partial or preliminary report cannot fabricate a
resolved outcome.

## Evidence library

| Evidence | Scope and limitation |
|---|---|
| [v7 audit](docs/research/2026-10-05-v7-audit.md) | October baseline and corrections; prospective v7 profit is unproven |
| [CRPS pilot](docs/research/2026-09-04-crps-pilot.md) | 2,658 reconstructed fixed-lead forecasts; exploratory, not execution replay |
| [ML challenger](docs/research/2026-09-05-ml-challenger.md) | MAE improved 2.34% on the exploratory sample, interval included no gain, CRPS worsened; not promoted |
| [Historical SFO evaluation](docs/accuracy_evaluation_2026-07-06.md) | Older station/model-specific experiment; not current account ROI or universal forecast skill |
| [Rejected retune](docs/trading_retune_validation_2026-06-17.md) | Apparent improvement rejected as statistically weak |

Use proper scores such as CRPS, Brier/log loss and calibration alongside point
error. Evaluate independent calendar-date clusters and account/policy eras;
repeated scans and correlated city outcomes are not independent observations.
Readiness evidence belongs to Live Stability, while research histories remain
separate.

## Local setup and verification

Use Python 3.12 or 3.13 (the project minimum is 3.11):

```bash
python3.13 -m venv .venv-dev
.venv-dev/bin/python -m pip install -e '.[dev]'
bash scripts/run_tests.sh
bash scripts/verify_project.sh
```

The full Python gate runs the repository health check, Semgrep, the backend and
forecaster suites, and compilation. Local Semgrep absence produces a warning;
CI requires the scan. These checks do not build or validate the web app.

Analysis commands that place no orders, from the repository root:

```bash
.venv-dev/bin/python -m sfo_kalshi_quant.cli --no-color analyze --target-date rolling --side both --cities all
.venv-dev/bin/python -m sfo_kalshi_quant.cli --no-color paper-report
.venv-dev/bin/python -m sfo_kalshi_quant.cli --no-color backtest-signals --sample-mode entry-per-market-side
```

The root `pyproject.toml` is the sole Python install manifest. It owns the
`sfo_kalshi_quant` package and `sfo-kalshi` console entrypoint. Heavy model
training uses `.[train]` locally; production uses lightweight dependencies.
See [forecaster instructions](forecaster/README.md) and
[trading instructions](trading/README.md) for data and command boundaries.

## Web app

The public site is the root React + TypeScript + Vite + HeroUI Pro SPA (`src/`).
Licensed components require `HEROUI_KEY` and the version-preserving installer
used in [CI](.github/workflows/verify.yml); plain package installation supplies
stubs and may not reproduce the app.

```bash
bun install --frozen-lockfile --ignore-scripts
env -u CI npx -y hpsetup@4.5.0 --auto
git diff --exit-code -- package.json bun.lock
bun install --frozen-lockfile
bun run lint
bun run test
bun run build
bun run preview --host 127.0.0.1 --port 4173
```

Check desktop and mobile behavior in a browser against that `dist/` build.
Initial browser-observed resource capture and budgets are separate verification gates:

```bash
bun run bundle:report
bun run bundle:capture -- /tmp/weatheredge-initial-resources.txt
bun run bundle:check:observed -- /tmp/weatheredge-initial-resources.txt
```

The SPA fetches runtime JSON at load. AWS publishes prebuilt assets plus fresh
`trading_signal.json`, `forecast_data.json`, `weather_story_data.json`,
`strategy_research.json`, and `cities_data.json` to GitHub Pages. Freshness and
hashes come from `publication_manifest.json`; HTTP 200 alone proves no data
freshness.

## Repository and runtime authority

Source belongs to Git; production weather/paper databases and current artifacts
belong to AWS. Ignored MacBook state can be stale. Before local dashboard design
checks, clear disposable local runtime state with the canonical helper:

```bash
python3 scripts/clear_local_runtime_state.py --confirm
```

This is a local cleanup, not a production data migration. Keep raw data,
credentials, private exports, keys, and operator details out of Git. Preserve
attribution and dated research records. The
[repository map](docs/REPOSITORY_MAP.md) identifies canonical entrypoints,
intentional mirrored files, and compatibility wrappers. Use the
[deployment runbook](docs/aws_deployment.md) for clean-revision deployment,
verified backups, account reconciliation, timer recovery, and publication
verification.

MIT — see [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).

## Local compute and cost

V7 supports a two-hour Mac research worker for fresh read-only paper/forecast
audits and calibration comparisons, with preserved results and job-scoped
`caffeinate`. AWS continues collection and paper execution. Full local replicas
use transaction-aware SQLite copying; they do not bypass the durable deployment
backup gate. See [local compute operations](docs/local_compute.md) for setup,
verification and the distinction between accrued bills and monthly cost.
