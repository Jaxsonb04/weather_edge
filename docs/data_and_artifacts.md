# Data And Artifacts

## Tracked Inputs And Fixtures

- Forecaster and trading source.
- `forecaster/ab_test_results.json` and
  `forecaster/model_compare_results.json`, the committed model-comparison inputs.
- `forecaster/forecast_data.json` and
  `forecaster/weather_story_data.json`, intentionally tracked public dashboard
  fixtures. Update them only as an explicit fixture change; do not mistake them
  for production runtime truth.
- Public fixture copies under `public/` used by local builds and missing-live-data
  fallbacks.

`model_compare_results.json` and `ab_test_results.json` are manually reviewed
research outputs. Together they are used to manually produce
`public/diagnostics.json`; that file is not an automatic training pipeline. No
files under `forecaster/models/` are committed.

For a machine that must keep a tracked fixture locally unchanged during normal
development, an operator may use `git update-index --skip-worktree <path>` and
later reverse it with `--no-skip-worktree`. This is local state only and must not
replace review of intentional fixture updates.

## Local Ignored Raw Data

The raw KSFO NOAA station history under
`forecaster/2016-2026 weather data/` is a local rebuild/research input. It is
untracked by design and ignored at the repository root; preserve it locally,
but do not treat it as a committed project input or production runtime artifact.

## Ignored Runtime State

Live databases, API caches, publication output, logs, virtual environments,
training intermediates, and `trading/data/` are ignored. In particular:

- `forecaster/weather.db`
- `forecaster/google_weather_cache.json`
- `forecaster/trading_signal.json`
- `forecaster/strategy_research.json`
- `forecaster/cities_data.json`
- `forecaster/publication_manifest.json`
- `forecaster/dataset_research.json`
- `trading/data/`

## Runtime Authority

After sync and refresh, the EC2 host is authoritative. Relevant paths are:

```text
/opt/weatheredge/forecaster/weather.db
/opt/weatheredge/forecaster/google_weather_cache.json
/opt/weatheredge/forecaster/trading_signal.json
/opt/weatheredge/forecaster/strategy_research.json
/opt/weatheredge/forecaster/cities_data.json
/opt/weatheredge/forecaster/publication_manifest.json
/opt/weatheredge/trading/data/paper_trading.db
/opt/weatheredge/trading/data/archive
/opt/weatheredge/webdist
```

See [AWS Deployment](aws_deployment.md). `sync_to_box.sh` and
`sync_forecaster_source.sh` share `forecaster-runtime.rsync-filter`, so local
stale runtime files, models, and the watchdog marker cannot overwrite or delete
EC2 state.

Before local dashboard verification:

```bash
python3 scripts/clear_local_runtime_state.py --confirm
bun run build
```

The cleanup writes explicit AWS-authority placeholders for ignored cache/signal
artifacts and removes other ignored publication output. It does not delete the
tracked dashboard fixtures.

## Archive Layer

Paper-journal retention uses
`/opt/weatheredge/trading/data/archive` by default. Daily gzip JSONL partitions
sit under per-table `dt=YYYY-MM-DD` paths. `manifest.db` records hashes, byte and
row counts, exact inclusive ID ranges, and decision-to-scan-context reference
coverage.

`run_archive_then_prune.sh` is the scheduled safety wrapper. It archives,
derives features, optionally uploads, requires exact-ID/reference coverage,
runs an explicit foreign-key audit, prunes, and finally cleans only verified
uploaded local partitions. Missing or corrupt archive evidence stops pruning.

S3 is optional and safe-off:

```text
SFO_ARCHIVE_DIR
SFO_ARCHIVE_KEEP_DAYS
SFO_ARCHIVE_S3_BUCKET
SFO_ARCHIVE_S3_PREFIX
SFO_ARCHIVE_AWS_CLI
```

Health gate:

```bash
python -m sfo_kalshi_quant.cli --no-color --db-path trading/data/paper_trading.db paper-archive --archive-dir trading/data/archive --check-gate
python -m sfo_kalshi_quant.cli --no-color --db-path trading/data/paper_trading.db paper-check-foreign-keys --limit 100
```

Restore into a new database while writers are stopped with the tested API:

```bash
PYTHONPATH=trading python -c 'from pathlib import Path; from sfo_kalshi_quant.archive import restore_archive_days; print(restore_archive_days(Path("trading/data/archive"), Path("trading/data/restored.db")))'
python -m sfo_kalshi_quant.cli --no-color --db-path trading/data/restored.db paper-check-foreign-keys --limit 100
```

The restore verifies partition hashes, inserts parents before children, and
rolls back on `PRAGMA foreign_key_check` failures. Never restore over the live
journal.
