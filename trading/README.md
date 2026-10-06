# WeatherEdge Prediction-Market Engine

The Python engine converts station-aligned forecasts into bracket probabilities,
compares them with real Kalshi public orderbooks, applies exact fees and bounded
risk, and records simulated execution in separate paper ledgers. The root
`pyproject.toml` is the sole install manifest and owns the `sfo-kalshi` command.

The current source registry has twenty cities; the October 5, 2026 production
snapshot had fifteen on the September 4 backend. Always inspect the artifact's
source revision and policy version before treating source behavior as deployed.

## Canonical operating path

`portfolio-scan` is the scheduled entry path. `analyze`, `tail-basket`, `arbitrage`
and `edge-scan` are diagnostics/research tools with separate purposes.
`deploy/aws/run_paper_scan_profiles.sh` orchestrates the configured live and
research profiles through the portfolio allocator.

The forecaster adapter reads the station/date forecast and its coupled
probability distribution from the forecaster runtime. SFO also retains legacy
blend/calibration adapters. An EMOS point cannot be priced with another model's
residual law. The market ladder is a useful prior and diagnostic; model-market
agreement alone does not prove independent weather skill or economic edge.

Entry, monitoring and settlement follow the original economic account identity.
Archive histories remain visible, including zero-trade policies. v7 begins a
separate paper research account and freezes v6 admission; existing v6 positions
continue their monitoring/settlement lifecycle. Past results are not relabeled
as v7 results.

## Evidence and objectives

The [v7 audit](../docs/research/2026-10-05-v7-audit.md) records account-scoped
baseline returns, requested versus filled volume, deployment gaps, concentrated
losses, calibration/replay limits, and tested correctness changes. Neither the
5% daily research objective nor $40/day by October 31 is a demonstrated run
rate. Resolved capital and initial capital are different ROI denominators.

Inspect after-fee net P&L, initial-capital returns, daily/date-cluster uncertainty,
drawdown, calibration, fill capture, slippage, and actual available depth.
A high profitable-exit rate and many repeated scans do not establish returns.

## Read-only local commands

From the repository root after installing `.[dev]`:

```bash
.venv-dev/bin/python -m sfo_kalshi_quant.cli --help
.venv-dev/bin/python -m sfo_kalshi_quant.cli --no-color analyze --target-date rolling --side both --cities all
.venv-dev/bin/python -m sfo_kalshi_quant.cli --no-color --risk-profile research analyze --target-date rolling --side both
.venv-dev/bin/python -m sfo_kalshi_quant.cli --no-color paper-report
.venv-dev/bin/python -m sfo_kalshi_quant.cli backtest-signals --sample-mode entry-per-market-side
.venv-dev/bin/python -m sfo_kalshi_quant.cli backtest-calibration --source clean-blend
```

`rolling` covers each city's today through today+2. `--cities sfo,lax` narrows
analysis. A read-only analysis can fetch market data; it does not reproduce
historical quotes or fill simulation. Calibration backtests have source-specific
samples. The legacy LSTM study and clean prior-day blend do not establish the
skill of every currently served multi-city distribution.

`daily-report` produces dashboard input without placing orders or recording new
decision snapshots. Frequent Strategy publication can reuse cached historical
analysis, which has its own timestamp; a new publication timestamp does not
make the readiness rescore current.

## Paper mutations and risk

Use a disposable local journal for experiments. `--place-paper`,
`portfolio-scan --place-paper`, `paper-monitor`, `paper-close`, and settlement
commands mutate paper evidence. Stake overrides remain subject to account,
exact fee, edge, quote, capacity and portfolio checks. They do not guarantee the
requested dollar amount fills.

Reservation-price maker limits require tape evidence to simulate fills; the
proxy does not reconstruct real queue priority. Configured taker crosses use
bounded whole-contract orderbook depth and exact fees. Shared bid depth must
not be reused independently by multiple exit lots; recorded quote time must
remain distinguishable from execution time.

Same-day observed highs rule out impossible lower outcomes but remain nonfinal.
Auto-settlement uses each city's station-keyed final CLI after its settlement
grace window. Prefer `paper-auto-settle` and the durable truth path. Manual
`paper-settle --settlement-high` is an operator override that requires the correct
station, target date, series and admissible official final high. A pending
preliminary report must remain pending.

Live Stability contributes to readiness. Research ROI and all archives do not.
The `live` profile name is a paper-readiness role: real-money execution remains
unimplemented and rejects non-dry orders.

## Source map

| Boundary | Canonical source |
|---|---|
| Command facade and handlers | `sfo_kalshi_quant/cli.py`, `_cli/` |
| Forecast and final truth | `forecast.py`, `settlement_truth.py` |
| Market parsing/quotes | `kalshi.py`, `orderbook_capture.py`, `consensus.py` |
| Probability and edge | `probability.py`, `strategy.py`, `risk.py` |
| Portfolio and sizing | `portfolio.py`, `research_portfolio.py`, `research_entry_risk.py` |
| Simulated order lifecycle | `execution.py`, `maker_fills.py`, `paper.py`, `monitor.py` |
| Accounting and identity | `account.py`, `logical_positions.py`, `paper_pnl.py` |
| Storage facade | `db.py` delegating to `store/` |
| Research replay/promotion | `research_replay.py`, `research_walkforward.py`, `research_promotion.py` |
| Versioned research policy | `research_policy.py`, `research_goals.py` |
| Strategy artifact facade | `strategy_research.py` delegating to `strategy_lab/` |
| Full deployment | `deploy/aws/sync_to_box.sh` |

Facade modules preserve stable imports/CLI contracts while implementations live
in submodules; similar filenames are not independent duplicate engines. See
[the repository map](../docs/REPOSITORY_MAP.md),
[strategy details](docs/strategy.md), and
[the user guide](docs/user_guide.md). Run `bash scripts/run_tests.sh` from the
root; deployment and live runtime checks are separate gates.
