# Operational Runbook

## Local Verification Gate

Run this before GitHub sync, AWS sync, dashboard publishing, or any larger code
change:

```bash
cd /path/to/WeatherEdge
bash scripts/verify_project.sh
```

This checks required WeatherEdge paths, local secret files, high-confidence
token patterns, trading tests, and Python syntax. Warnings about Git or optional
quality tools are useful setup reminders; failures should be fixed before sync
or deploy work continues.

## GitHub Hygiene Check

Run this before merging audit or deployment work:

```bash
cd /path/to/WeatherEdge
python3 scripts/github_hygiene_check.py
```

The checker is read-only. It verifies public branch-protection status for
`main` and `gh-pages`, reports stale or stacked open PRs, and lists stale remote
branches from local `origin/*` refs. Use a `GITHUB_TOKEN` only if public API rate
limits or private settings require authenticated reads.

## Local Forecast Refresh Without Google API

```bash
cd /path/to/WeatherEdge/forecaster
python nws_ground_truth.py --days 14
python google_weather_cache.py
```

This reuses cached Google Weather and refreshes public/free context.

## Local Forecast Refresh With Google API

```bash
cd /path/to/WeatherEdge/forecaster
export GOOGLE_WEATHER_API_KEY="..."
python google_weather_cache.py --refresh
python google_weather_cache.py
```

Check the budget fields in `google_weather_cache.json` after refresh.

## Paper Analyze

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both
```

`analyze` loops all fifteen registered cities by default (env `PAPER_CITIES`,
default `all`). Pass `--cities` with `all` or a comma list of slugs to
override:

```bash
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both --cities sfo,lax
```

Production entry mode is maker-first resting limits (`PAPER_ENTRY_MODE=limit`);
resting quotes pay the maker fee, and the 2-minute monitor fills a resting
limit when the visible ask crosses it (a proxy fill model, no queue position):

```bash
PAPER_ENTRY_MODE=limit python -m sfo_kalshi_quant.cli --no-color analyze --target-date rolling --side both --place-paper
```

## Portfolio Paper Scan

Scheduled AWS paper placement uses the shared allocator and loops all cities
(`PAPER_CITIES=all` in production; `--cities` narrows it for diagnostics):

```bash
cd /path/to/WeatherEdge/trading
python -m sfo_kalshi_quant.cli --no-color portfolio-scan --target-date rolling --side both
```

To record approved paper portfolio orders:

```bash
python -m sfo_kalshi_quant.cli --no-color portfolio-scan --target-date rolling --side both --place-paper
```

The allocator funds guaranteed arbitrage first, then high-confidence NO core,
capped YES convex exposure, and research-only exploration when the profile
allows it. The commands below are diagnostics and should not replace the
scheduled portfolio path.

## Paper Arbitrage Diagnostic

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color arbitrage --target-date rolling --max-arb-spend 12
```

To record approved paper arbitrage portfolios:

```bash
python -m sfo_kalshi_quant.cli --no-color arbitrage --target-date rolling --max-arb-spend 12 --place-paper
```

This scans all active temperature bins for the target day. Same-bin YES+NO boxes
and full-ladder YES/NO sets are paper-placed only when the guaranteed payout is
above all-in cost after rounded fees.

## Public Paper Research Artifact

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color daily-report --target-date both --side both --format json --no-live-market --output forecaster/trading_signal.json
python -m sfo_kalshi_quant.cli --no-color strategy-research --output forecaster/strategy_research.json
```

This is read-only. It does not record snapshots, place paper orders, or expose
private DB state. In production, `sfo-operational-publish.timer` runs every five
minutes: `build_public_trading_signal.sh` generates
`forecaster/trading_signal.json`, `forecaster/cities_data.json`, and
`forecaster/publication_manifest.json`, then the publisher validates and ships
that snapshot alongside the SPA. The research-only
`sfo-strategy-lab-refresh.timer` runs every fifteen minutes to rebuild
`forecaster/strategy_research.json` separately without calling the paid Google
Weather refresh command. No published artifact contains private DB state; the
trading artifacts contain only paper-trading research. `cities_data.json`
supplies per-city forecasts, latest settlement, and book activity for the
fifteen-city Coverage grid.

## Paper Place

```bash
python -m sfo_kalshi_quant.cli --no-color analyze --target-date today --paper-stake 10 --place-paper
```

Only rows that pass all risk gates are recorded.

## Paper Monitor

```bash
python -m sfo_kalshi_quant.cli --no-color paper-monitor \
  --yes-take-profit-pct 50 --yes-stop-loss-pct 25 \
  --no-take-profit-pct 35 --no-stop-loss-pct 35 \
  --model-veto-max-loss-pct 60 --model-veto-buffer 0.08
```

## Paper Settle

```bash
python -m sfo_kalshi_quant.cli --no-color paper-settle --target-date YYYY-MM-DD --settlement-high 67
```

Replace `67` with the official resolved high for that city and date.
Settlement is series-scoped: one city's high can never settle another city's
bins.

AWS can also settle automatically. Auto-settle reads only durable rows from
`weather.db` whose `cli_settlements.is_final=1`; it never books a raw live CLI
response. A target becomes eligible at 06:00 on the next fixed-standard
settlement day, and remains open if confirmed-final truth has not arrived:

```bash
python -m sfo_kalshi_quant.cli --no-color paper-auto-settle
```

Audit recently booked settlements without rewriting their P&L or outcome:

```bash
python -m sfo_kalshi_quant.cli --no-color paper-resettle --verify --days 14
```

The sweep records `MATCH`, `MISMATCH`, and `MISSING_FINAL` results in
`paper_settlement_verifications` using each city's fixed-standard date window.

## Edge Scan Diagnostic

`sfo_kalshi_quant/edge_scan.py` measures the favorite-band maker opportunity
on live order books. Its first run posted 51 quotes across 16 city-days with a
median model edge of +0.8c and a maker-vs-taker saving of 1.45c per contract;
roughly 619 settled trades are needed to confirm a 2.6% mean edge at 95%
confidence.

## Scheduled Multi-City Refresh And Nightly Maintenance

On AWS, the 30-minute forecaster refresh serves live EMOS forecasts for all
fifteen cities (one batched Open-Meteo call per city) plus NWS observations
(`--days 2 --cities all`).

The nightly dataset unit (02:25 Pacific) additionally runs:

- IEM CLI settlement-truth refresh
- NWP archive update (`--daily --cities all`, scheduled leads 1 and 2 only)
- EMOS rolling-origin rebuild (leads 1 and 2)

Lead 3 is research/on-demand only. Preserve it in explicit historical
`nwp_archive.py --backfill --start ... --end ...` runs, but do not add it back
to the nightly `--daily` job.

## Archive-Gated Paper Retention

Production retention belongs only to the dedicated
`sfo-kalshi-paper-prune.timer` / `sfo-kalshi-paper-prune.service`. The service
runs `trading/deploy/aws/run_archive_then_prune.sh`, which losslessly archives
and verifies every complete UTC day before its final prune step. If archival or
the explicit archive gate fails, pruning does not run.

Production sets `SFO_PRUNE_FULL_DAYS=1`; last-per-market-side-day rows remain for
45 days and approved rows remain indefinitely. Fifteen cities otherwise write
roughly 60k rejection snapshots (~0.5 GB) per day. Do not schedule or routinely
run bare `paper-prune`: it is a low-level/manual command for recovery work only,
after an operator has independently completed and verified the archive gate.

## Signal Backtest

```bash
python -m sfo_kalshi_quant.cli --no-color backtest-signals
python -m sfo_kalshi_quant.cli --no-color backtest-signals --min-quality 60
```

This scores recorded decision snapshots against official settled highs. Use
it to check rejected rows, approved rows, and quality buckets before trusting a
new gate profile.
