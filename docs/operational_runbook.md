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

To simulate conservative paper buy limits instead of immediate paper fills:

```bash
PAPER_ENTRY_MODE=limit python -m sfo_kalshi_quant.cli --no-color analyze --target-date rolling --side both --place-paper
```

## Portfolio Paper Scan

Scheduled AWS paper placement uses the shared allocator:

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
private DB state. In production, `build_public_trading_signal.sh` generates
`forecaster/trading_signal.json` and `forecaster/strategy_research.json`, and
the publisher ships both as plain public JSON alongside the SPA site. They
contain only paper-trading research data. The `sfo-strategy-lab-refresh.timer`
refreshes this trading-results path every five minutes without calling the paid
Google Weather refresh command.

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

Replace `67` with the official resolved SFO high.

AWS can also settle the latest published CLISFO report automatically:

```bash
python -m sfo_kalshi_quant.cli --no-color paper-auto-settle
```

## Signal Backtest

```bash
python -m sfo_kalshi_quant.cli --no-color backtest-signals
python -m sfo_kalshi_quant.cli --no-color backtest-signals --min-quality 60
```

This scores recorded decision snapshots against official settled SFO highs. Use
it to check rejected rows, approved rows, and quality buckets before trusting a
new gate profile.
