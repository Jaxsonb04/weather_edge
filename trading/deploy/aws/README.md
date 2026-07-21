# AWS EC2 Deployment

These scripts operate the always-on WeatherEdge EC2 runtime at
`/opt/weatheredge`. The current host is Ubuntu arm64 on a `t4g.medium` in
`us-west-1` (migrated from `us-east-1` on 2026-07-11). This directory supports
deployment and local verification; it does not authorize production access or
changes.

## Runtime Contract

- `sync_to_box.sh` is the operator-driven full source sync. It defaults to
  `.local/ec2.env`, prefers `EC2_IP`/`EC2_KEY`, sends the root
  `pyproject.toml`/`README.md` install inputs plus both source trees, and
  preserves remote runtime state. It first proves that the live database can
  be backed up to and restored from S3. Before the first remote tree mutation
  or source transfer, it streams the canonical timer/service helper to the host
  and captures the enabled timer set before quiescing every WeatherEdge timer
  and paired service. A transfer or install failure intentionally leaves them
  quiesced for a clean retry. After all transfers succeed, it removes only the
  retired nested manifest, two stale service templates, and eleven audited
  pre-`research/` script paths; it never broadly deletes either runtime tree.
  It then runs the timerless installer and restores exactly the timers that were
  enabled before the deploy. A successful sync can no longer exit with an
  established host silently disabled, while intentional per-timer pauses remain
  intact.
- `sync_to_lightsail.sh` is a deprecated forwarding-only compatibility wrapper
  for the EC2 migration window. New commands must use `sync_to_box.sh`.
- `pull_paper_db.sh` allocates a private mode-700 directory on the remote host,
  writes and verifies a mode-600 SQLite backup there, verifies the downloaded
  copy, and removes the complete temporary directory before publishing the
  local database atomically.
- `sync_forecaster_source.sh` is the scheduled, source-only Git refresh. It uses
  `--delete` for tracked forecaster source but shares
  `forecaster-runtime.rsync-filter` with the full sync.
- The intentional difference is scope: full sync sends the root packaging
  inputs and both local source trees without deleting remote-only files; source
  sync refreshes only the committed `forecaster/` subtree from `main`. The
  scheduled source sync never reinstalls the trading environment.
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

Provision the encrypted, versioned backup target from an operator shell with
AWS infrastructure credentials before the first deploy:

```bash
bash trading/deploy/aws/provision_backup_bucket.sh \
  "weatheredge-paper-backups-$(aws sts get-caller-identity --query Account --output text)-$EC2_REGION" \
  "$EC2_REGION" "$EC2_INSTANCE_ID"
```

Copy the three printed `SFO_...` values into `/etc/weatheredge.env` and install
AWS CLI v2 with Amazon's official Linux ARM installer. Ubuntu 24.04 ARM has no
`awscli` apt candidate. Verify `aws sts get-caller-identity` resolves to the
instance role. The deployment script does not provision IAM or S3.

On an established host, the full sync refuses to stop services until
`backup_paper_db.sh preflight` proves the configured AWS identity and bucket
are available. After quiescing, it creates a consistent SQLite backup, checks
integrity and foreign keys, uploads it with server-side encryption, downloads
it to a temporary restore path, and repeats those checks. Only then does the
source transfer begin. The full sync reinstalls units and restores the exact
pre-deploy timer policy automatically. On a new or intentionally quiesced host,
the captured set is empty and every timer remains disabled for manual checks.

## Install Modes

Use the no-timers installer directly for a new host, migration, or recovery:

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
set. Normal established-host source deployments use `sync_to_box.sh`, which
invokes the timerless installer as its deployment gate and restores the captured
timer set only after that gate succeeds.

Both modes keep the trading virtual environment at
`/opt/weatheredge/trading/.venv`, but install the sole editable Python project
from `/opt/weatheredge`, where the full sync places `pyproject.toml` and its
`README.md` build input. An upgrade first uninstalls the retired
`sfo-kalshi-quant` distribution, then verifies through package metadata that
`weatheredge` is the sole project owner and still provides `sfo-kalshi`.
The migration also removes the exact generated
`trading/sfo_kalshi_quant.egg-info` directory that legacy editable uninstalls
leave in the source tree, plus the transient `trading/weatheredge.egg-info`
created while building the replacement editable wheel. Verification requires
exactly one matching distribution metadata object and exactly one console entry.
Before pip runs, both installers normalize the trading virtualenv back to the
configured app user. The project installer also removes only pip's exact
interrupted `~eatheredge-*.dist-info` temporary metadata inside that verified
virtualenv, preventing an older privileged install from appearing as a second
WeatherEdge distribution.
Installers refuse to proceed if the obsolete `trading/pyproject.toml` survives,
so a partial or manual sync cannot recreate split ownership.
The full sync accepts only canonical conservative absolute `REMOTE_BASE` paths:
no root path, repeated or trailing slash, or `.`/`..` component reaches SSH or
rsync.

The forecaster runtime installs only `certifi numpy pandas`; the correctly
formed command is a hash-verified install from `requirements/production.lock`. Heavy training
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
Full-database deployment backups use the same bucket with
`SFO_DATABASE_BACKUP_S3_PREFIX`; local verified copies are retained according
to `SFO_DATABASE_BACKUP_KEEP_DAYS`.
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
The watchdog rejects local operational artifacts older than 15 minutes,
public operational artifacts or Strategy Lab research older than 20 minutes, disk
usage at or above 85%, missing files,
invalid schemas, and checksum mismatches. It writes `STALE_FORECAST` for the
local alarm path; sync excludes preserve that marker. Every operational service
also routes failures through `sfo-alert@.service`, which posts JSON to
`SFO_FRESHNESS_ALERT_URL` without putting the endpoint in process arguments.
The watchdog never posts directly: systemd gets one common JSON alert, while a
manual run reports locally without duplicating the webhook.
During a full deploy, `wait_for_publication_manifest.sh` polls for the exact
local snapshot ID and source SHA before the watchdog is started or restored, so
normal GitHub Pages propagation cannot produce a false stale alarm.

The workstation web deploy uses rsync 3.x `--protect-args` when available.
Apple openrsync remains supported for the shell-safe default remote base; an
unprotected base must match `^/[A-Za-z0-9._/-]+$` and contain no `..` path
component. Anything else is rejected before build or SSH. A temporary no-space
SSH wrapper keeps spaced key paths intact in both modes.
All rsync modes reject root and noncanonical aliases (repeated/trailing slashes
or `.`/`..` components) before build. Protect-args mode continues to permit
spaces within otherwise canonical path components.

The canonical environment reference is `sfo-weather.env.example`. It contains
safe defaults for the five live-execution gates, publication paths and locks,
dataset paths, rolling targets/cutoff, archive/S3 settings, and the Batch C
same-day heartbeat.

See [`../../../docs/aws_deployment.md`](../../../docs/aws_deployment.md) for host
details, security-group policy, and operator recovery.
