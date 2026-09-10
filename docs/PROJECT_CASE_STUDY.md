# WeatherEdge: Engineering Case Study

**Read time: about three minutes.**

[Live dashboard](https://jaxsonb04.github.io/weather_edge/) ·
[Source](https://github.com/Jaxsonb04/weather_edge) ·
[Architecture](architecture.md) ·
[Model evidence](accuracy_evaluation_2026-07-06.md) ·
[Strategy Lab](https://jaxsonb04.github.io/weather_edge/#/lab)

## The Problem

A weather prediction market does not ask for a generic city forecast. It settles
one station's daily high into a discrete temperature bracket, on that station's
local-standard climate day. A useful system therefore has to do more than predict
a temperature: it must preserve forecast vintages, calibrate uncertainty, map a
continuous distribution into settlement bins, price fees and liquidity, enforce
risk, settle against independent truth, and explain the result.

WeatherEdge implements that entire loop for fifteen U.S. city markets, with San
Francisco as the deepest research case. It is intentionally paper-only.

## The System

| Stage | Engineering work |
|---|---|
| Inputs | Point-in-time NWP archives, station observations, official NWS climate reports, and public prediction-market books |
| Forecasting | Eight NWP members feed rolling-origin EMOS models per station; SFO adds separate LSTM/XGBoost and optional-source research |
| Probability | Gaussian and empirical uncertainty are converted into mutually exclusive settlement-bin probabilities |
| Decisions | Model probabilities are compared with de-vigged market prices after fees, spread, liquidity, confidence, and exposure gates |
| Execution research | Two isolated paper accounts model reservation limits, bounded crosses, fills, exits, and settlement without sending live orders |
| Operations | AWS EC2 and systemd timers run the pipeline unattended; watchdogs, backups, provenance checks, and atomic publication fail closed |
| Product | A React/TypeScript dashboard exposes live forecasts, methodology, account-scoped paper evidence, data freshness, and limitations |

## Design Decisions That Matter

### Time leakage is treated as a correctness bug

Historical NWP inputs are accepted only from model cycles that existed before
the forecast target. Model evaluation uses rolling-origin splits, and clean
next-day scoring excludes same-day observed-high adjustments. This prevents a
good-looking backtest from learning information unavailable at decision time.

### Settlement truth is independent of the forecaster

The system does not grade a forecast with its own observations. Every market is
mapped to a station-specific NWS Climatological Report; only confirmed final
records can settle paper positions. City identity, timezone, station, market
series, and climate-day boundaries travel together through the pipeline.

### Risk is a gate, not a presentation layer

Candidate trades must clear exact fees, liquidity, model/market disagreement,
lower-confidence-bound edge, per-market and portfolio exposure, daily loss, and
drawdown controls. Missing or stale evidence produces no trade. The dormant
live path has no authenticated client and raises `LiveTradingDisabled`.

### Research accounts are not combined

Live Stability is the conservative readiness cohort. Research ROI is a separate,
bounded experiment. Their balances, positions, and P&L remain economically
separate in storage, publication, and UI; archived policy eras are labeled as
historical evidence rather than blended into a flattering total.

### Operational failures are part of the project

The repository records incidents and fixes, including SQLite lock-hold failures,
retention jobs that exceeded their service window, publication lag, and browser
bundle regressions. Deployment is gated by recoverable database backups,
integrity checks, source provenance, timer restoration, and public artifact
verification. The point is not to claim perfect uptime; it is to show how the
system fails and how recovery is proved.

## Evidence, Not Claims

- **SFO daily-high model:** 442 held-out days; LSTM MAE 3.12°F versus 3.71°F
  for the paired XGBoost challenger, with Diebold–Mariano p < 0.001.
- **SFO probability engine:** 262 scored out-of-sample settlements; 45.4%
  ranked-probability skill over climatology and 29.5% Brier skill.
- **Historical depth:** 3,419 observed KSFO days across ten years.
- **Current scope boundary:** the deepest LSTM and optional-input evidence is
  SFO-only. The other fourteen cities use the shared NWP→EMOS path and have a
  shorter operational record.
- **Verification:** CI runs Python 3.12 and 3.13, 141 Python test files, 178
  frontend tests, Semgrep, lint, production builds, deterministic icon checks,
  and a browser-observed JavaScript/CSS budget.

These metrics are research evidence, not promised returns or proof that every
city has the same forecast skill. The public dashboard reports current runtime
state; dated production facts in the repository are explicitly labeled as
snapshots.

## Where to Review the Work

| If you care about… | Start here |
|---|---|
| The product | [Live dashboard](https://jaxsonb04.github.io/weather_edge/) |
| System boundaries | [`docs/architecture.md`](architecture.md) |
| A full code map | [`docs/CODEBASE-WALKTHROUGH.md`](CODEBASE-WALKTHROUGH.md) |
| Statistical evaluation | [`docs/accuracy_evaluation_2026-07-06.md`](accuracy_evaluation_2026-07-06.md) |
| Trading/risk design | [`trading/docs/strategy.md`](../trading/docs/strategy.md) |
| Production operations | [`docs/aws_deployment.md`](aws_deployment.md) and [`docs/operational_runbook.md`](operational_runbook.md) |
| AI workflow and safeguards | [`docs/ai-assisted-development.md`](ai-assisted-development.md) |
| Current dated handoff | [`docs/SESSION_MEMORY.md`](SESSION_MEMORY.md) |

## Current Boundary

WeatherEdge is a public engineering and research project, not a trading product.
It reads public market data, writes simulated orders, and publishes paper results.
Real-money execution remains unimplemented and fail-closed. That constraint is a
feature of the system's evidence standard, not an omitted disclaimer.
