# AWS EC2 Deployment

These scripts operate the always-on WeatherEdge EC2 runtime at
`/opt/weatheredge`. The current host is Ubuntu arm64 on a `t4g.medium` in
`us-east-1`. This directory supports deployment and local verification; it does
not authorize production access or changes.

## Runtime Contract

- `sync_to_box.sh` is the operator-driven full source sync. It defaults to
  `.local/ec2.env`, prefers `EC2_IP`/`EC2_KEY`, and preserves remote runtime
  state.
- `sync_to_lightsail.sh` is a deprecated forwarding-only compatibility wrapper
  for the EC2 migration window. New commands must use `sync_to_box.sh`.
- `pull_paper_db.sh` allocates a private mode-700 directory on the remote host,
  writes and verifies a mode-600 SQLite backup there, verifies the downloaded
  copy, and removes the complete temporary directory before publishing the
  local database atomically.
- `sync_forecaster_source.sh` is the scheduled, source-only Git refresh. It uses
  `--delete` for tracked forecaster source but shares
  `forecaster-runtime.rsync-filter` with the full sync.
- The intentional difference is scope: full sync sends both local source trees
  without deleting remote-only files; source sync refreshes only the committed
  `forecaster/` subtree from `main`.
- Runtime DBs and their SQLite `-wal`/`-shm` sidecars, publication JSONs,
  `STALE_FORECAST`, and `models/` are never clobbered. The tracked
  `forecast_data.json` and `weather_story_data.json` inputs are intentionally
  copied by both sync paths.
- The served committed model-evaluation input is
  `forecaster/ab_test_results.json`. There are zero committed files under
  `forecaster/models/`.

Configure the ignored local file:

```bash
EC2_IP=replace_with_public_ip
EC2_KEY=/absolute/path/to/deploy-key.pem
REMOTE_USER=ubuntu
```

Then sync and connect:

```bash
bash trading/deploy/aws/sync_to_box.sh
source .local/ec2.env
ssh -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP"
```

## Install Modes

Use the no-timers installer for a new host, migration, or recovery:

```bash
cd /opt/weatheredge/trading
bash deploy/aws/install_systemd_notimers.sh
```

Both installers begin with a read-only timezone preflight. The regular installer
refuses to mutate a host that is not already on `America/Los_Angeles`. The
timerless installer first quiesces the complete timer/service set, then changes
a mismatched timezone before installing dependencies and units. Preflight,
inspection, stop, disable, timezone-set, or quiescence failures propagate. After
manual service checks, use `install_systemd.sh` for the established full timer
set.

The forecaster runtime installs only `certifi numpy pandas`; the correctly
formed command is `python -m pip install certifi numpy pandas`. Heavy training
dependencies do not belong on the production box.

## Cadence And Responsibilities

- Forecast refresh: twice hourly from 05:10 through 18:40 PT and hourly
  overnight; all fifteen cities, SFO flagship.
- Operational publication: every five minutes; builds
  `trading_signal.json`, `cities_data.json`, and `publication_manifest.json`.
- Strategy Lab publication: every 15 minutes; research-only artifact build.
- Paper scan: every five minutes, all configured cities, two profiles
  (`PAPER_RISK_PROFILES=live,research`), maker-first with
  `PAPER_ENTRY_MODE=limit` and `PAPER_CITIES=all`.
- Paper monitor: every two minutes.
- Dataset/backfill: nightly, including NWP leads 1 and 2. Lead 3 is manual.

Publication is finality-aware and race-safe: builders share
`SFO_ARTIFACT_GENERATION_LOCK`; the publisher validates the manifest before it
copies exact artifacts, then serializes Git work with `SFO_PAGES_LOCK`.
Both publisher and paper-scan locks default under `/opt/weatheredge/.locks` so
reboots clean temporary storage without weakening overlap protection. Configure
the deploy key as `/home/ubuntu/.ssh/sfo_weather_pages_deploy` and the Git source
as `git@github.com:Jaxsonb04/weather_edge.git`.

## Archive-Gated Retention

`sfo-kalshi-paper-prune.timer` runs `run_archive_then_prune.sh`, which:

1. Exports every complete UTC day into the archive directory.
2. Builds the derived feature store (non-fatal and rebuildable).
3. Uploads to S3 only when configured.
4. Requires the manifest's exact-ID and context-reference coverage gate.
5. Runs `paper-check-foreign-keys`.
6. Runs the low-level `paper-prune` command.
7. Removes old local partitions only after verified upload.

The default archive is `/opt/weatheredge/trading/data/archive`; the manifest is
`manifest.db`. Configure `SFO_ARCHIVE_DIR`, `SFO_ARCHIVE_KEEP_DAYS`,
`SFO_ARCHIVE_S3_BUCKET`, `SFO_ARCHIVE_S3_PREFIX`, and `SFO_ARCHIVE_AWS_CLI`.
Without a bucket, the local ring buffer remains authoritative and cleanup skips
unuploaded files.

Health and finality checks:

```bash
cd /opt/weatheredge/trading
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-archive --archive-dir data/archive --check-gate
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-check-foreign-keys --limit 100
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-resettle --verify --days 14
```

For an existing large journal, keep paper scan and monitor services paused and
run `create_decision_snapshot_index.sh` once before resuming them. It builds the
covering decision-report index without putting that expensive migration on
normal service startup.

Restore only to a new DB while paper services are stopped, using the tested
`restore_archive_days` API, then run `paper-check-foreign-keys` before any swap.

## Publication Health

Set
`SFO_PUBLICATION_MANIFEST_URL=https://jaxsonb04.github.io/weather_edge/publication_manifest.json`.
The watchdog rejects operational artifacts older than 10 minutes, Strategy Lab
research older than 20 minutes, disk usage at or above 85%, missing files,
invalid schemas, and checksum mismatches. It writes `STALE_FORECAST` for the
local alarm path; sync excludes preserve that marker. Every operational service
also routes failures through `sfo-alert@.service`, which posts JSON to
`SFO_FRESHNESS_ALERT_URL` without putting the endpoint in process arguments.
The watchdog never posts directly: systemd gets one common JSON alert, while a
manual run reports locally without duplicating the webhook.

The workstation web deploy uses rsync 3.x `--protect-args` when available.
Apple openrsync remains supported for the shell-safe default remote base; an
unsafe base containing whitespace, quotes, or backslashes is rejected before
building. A temporary no-space SSH wrapper keeps spaced key paths intact in
both modes.

The canonical environment reference is `sfo-weather.env.example`. It contains
safe defaults for the five live-execution gates, publication paths and locks,
dataset paths, rolling targets/cutoff, archive/S3 settings, and the Batch C
same-day heartbeat.

See [`../../../docs/aws_deployment.md`](../../../docs/aws_deployment.md) for host
details, security-group policy, and operator recovery.
