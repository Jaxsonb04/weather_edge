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

## Local Forecast Refresh Without Google API

```bash
cd /path/to/WeatherEdge/forecaster
python nws_ground_truth.py --days 14
python google_weather_cache.py
python build_dashboard.py
```

This reuses cached Google Weather and refreshes public/free context.

## Local Forecast Refresh With Google API

```bash
cd /path/to/WeatherEdge/forecaster
export GOOGLE_WEATHER_API_KEY="..."
python google_weather_cache.py --refresh
python google_weather_cache.py
python build_dashboard.py
```

Check the budget fields in `google_weather_cache.json` after refresh.

## Paper Analyze

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both
```

## Public Paper Research Artifact

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color daily-report --target-date both --side both --format json --no-live-market --output forecaster/trading_signal.json
python -m sfo_kalshi_quant.cli --no-color strategy-research --output forecaster/strategy_research.json
```

This is read-only. It does not record snapshots, place paper orders, or expose
private DB state. The generated `forecaster/trading_signal.json` and
`forecaster/strategy_research.json` are optional dashboard inputs.
AWS temporarily publishes plaintext Strategy Lab data while
`SFO_STRATEGY_LAB_PUBLIC_MODE=1`. Restore the password gate by setting
`SFO_STRATEGY_LAB_PUBLIC_MODE=0` and `SFO_STRATEGY_LAB_PASSWORD`; then
`forecaster/build_dashboard.py` writes
`forecaster/strategy_research.protected.json`, and the publisher ships that
protected artifact instead of plaintext Strategy Lab research data. The
`sfo-strategy-lab-refresh.timer` refreshes this trading-results path every five
minutes without calling the paid Google Weather refresh command.

## Paper Place

```bash
python -m sfo_kalshi_quant.cli --no-color analyze --target-date today --paper-stake 10 --place-paper
```

Only rows that pass all risk gates are recorded.

## Paper Monitor

```bash
python -m sfo_kalshi_quant.cli --no-color paper-monitor --take-profit-pct 35 --stop-loss-pct 35
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
