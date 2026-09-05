# AWS Deployment (EC2)

WeatherEdge production runs on an AWS EC2 `t4g.medium` in `us-west-1`
(migrated from `us-east-1` on 2026-07-11; the quiesced east instance awaits
owner decommission). The host is Ubuntu 24.04 arm64 (`ubuntu`), and the
application root is `/opt/weatheredge`. Live databases, caches, and dashboard
artifacts on that host are authoritative after sync and refresh.

The operator connection settings belong in the ignored `.local/ec2.env`:

```bash
EC2_IP=replace_with_public_ip
EC2_KEY=/absolute/path/to/deploy-key.pem
REMOTE_USER=ubuntu
```

Keep the key mode at `0600`. Never commit the env file or key.

The S3/IAM operator checkpoint is automated by
`trading/deploy/aws/provision_backup_bucket.sh BUCKET REGION INSTANCE_ID`. It
requires an AWS infrastructure identity, creates or configures the backup
bucket, and limits the instance role to the journal and full-database prefixes.
The host also needs AWS CLI v2. Ubuntu 24.04 ARM does not provide an `awscli`
apt candidate, so install the official AWS Linux ARM package and verify that
`aws sts get-caller-identity` resolves to the instance role. Copy the script's
printed `SFO_...` values into `/etc/weatheredge.env` before the first deploy.

## Deploy And Install

From the repository root:

```bash
bash trading/deploy/aws/sync_to_box.sh
source .local/ec2.env
ssh -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP"
```

On an established host, `sync_to_box.sh` first proves that the authoritative
database can be uploaded to and restored from the configured S3 backup target.
It is then transactional around scheduled work: it captures the enabled
WeatherEdge timers, quiesces the services, creates and independently restores a
checksummed SQLite backup, syncs the tree, runs `install_systemd_notimers.sh`
as the install gate, and restores
exactly the captured timer set. If transfer or installation fails, it exits
nonzero and leaves the host quiesced for a clean retry. A new or intentionally
quiesced host has an empty captured set and remains timerless for manual checks.

Both installers first read the host timezone without mutating it. The regular
installer proceeds only when it is already `America/Los_Angeles`; otherwise it
refuses and directs the operator to the cutover-safe timerless installer.
When invoked directly for provisioning or recovery,
`install_systemd_notimers.sh` quiesces every existing WeatherEdge timer and
paired service before changing a mismatched timezone, then renders every unit
while enabling none. A preflight failure changes nothing; a timezone-set
failure propagates only after services are safely quiesced. Inspect
`/etc/weatheredge.env`, start each service manually, and only then enable the
approved timers.

The full sync first streams the canonical timer-state helper to the host and
captures the enabled set before it stops/disables every WeatherEdge timer plus
its paired service ahead of any remote tree mutation or source transfer. It does
not assume the helper already exists in the old remote source tree. A failed
transfer or install remains safely quiesced; a successful deploy restores only
the captured timers. The full sync does not use `--delete`. Scheduled services
execute the exact source installed by this controlled full deploy; they never
fetch or replace source on their own. The former forecaster-only sync is a
disabled compatibility tombstone because it could split the deployed revision
across source trees. `forecaster-runtime.rsync-filter` preserves runtime
databases, caches,
their SQLite `-wal`/`-shm` sidecars, generated publication JSON,
`STALE_FORECAST`, and `models/`. The committed `forecast_data.json` and
`weather_story_data.json` inputs are deployed by the full sync; they are
source-controlled inputs, unlike their runtime-produced JSON siblings.
The full sync also deploys the root `pyproject.toml` and `README.md`; both
installers keep the executable environment under `trading/.venv` while running
the editable install from `/opt/weatheredge`. No timer, service, or recovery
helper mutates source between controlled deployments.
Generated `*.egg-info` metadata is excluded
from source transfer and recreated by the remote editable install. After every
full transfer succeeds, the sync removes only the obsolete
`trading/pyproject.toml`, the two retired service
templates under `trading/sfo_kalshi_quant/`, and the eleven audited top-level
forecaster scripts now housed under `forecaster/research/`. No runtime database,
raw input, model directory, or publication artifact is part of that cleanup.

During an upgrade, the installer uninstalls the retired `sfo-kalshi-quant`
distribution before installing `weatheredge`, then verifies that the old
distribution is absent and the `sfo-kalshi` console entry belongs to
`weatheredge`. It removes only the old generated
`trading/sfo_kalshi_quant.egg-info` metadata that pip's legacy editable
uninstall leaves behind and the transient `trading/weatheredge.egg-info`
created during the replacement build. Verification requires exactly one
WeatherEdge distribution metadata record and one correctly owned console
entry. Both installers normalize the trading virtualenv to the configured app
user before pip runs, and the project installer removes pip's exact interrupted
`~eatheredge-*.dist-info` temporary metadata only inside that verified
virtualenv. A surviving nested trading manifest is a hard preflight failure.

`sync_to_box.sh` rejects noncanonical `REMOTE_BASE` spellings before any remote
action, including root, repeated/trailing slashes, and `.` or `..` components.

During the EC2 migration window, `sync_to_lightsail.sh` remains as a deprecated
forwarding wrapper to `sync_to_box.sh`. It has no deployment logic of its own;
new operator commands and automation must use `sync_to_box.sh` directly.

## Runtime Layout

```text
/opt/weatheredge/forecaster
/opt/weatheredge/trading
/opt/weatheredge/pyproject.toml
/opt/weatheredge/README.md
/opt/weatheredge/trading/data/archive
/opt/weatheredge/webdist
/opt/weatheredge/.cache/main
/opt/weatheredge/.locks
/run/weatheredge/google_runtime.db
/run/weatheredge/apple_weather_runtime.json  # only while enabled and unexpired
```

Publication and paper-scan locks default under `/opt/weatheredge/.locks`, so
they persist independently of temporary-directory cleanup. The freshness
watchdog fails at 85% filesystem usage by default, and operational service
failures post JSON through the non-recursive `sfo-alert@.service` template when
`SFO_FRESHNESS_ALERT_URL` is configured. The watchdog itself never posts, so a
systemd failure produces exactly one JSON webhook and a manual run stays local.

`deploy_web_app.sh` uses rsync 3.x `--protect-args` when available. Apple's
legacy openrsync may deploy to the shell-safe default `/opt/weatheredge`, but
without protect-args `REMOTE_BASE` must match the conservative absolute-path
allowlist `^/[A-Za-z0-9._/-]+$` and contain no `..` component. Violations are
rejected before build or SSH. The deployer uses a temporary no-space SSH wrapper
and removes it on every exit, so SSH key paths may contain spaces in either mode.
Every rsync mode rejects root and noncanonical aliases (repeated/trailing
slashes or `.`/`..` components) before build; protect-args mode still permits
spaces within otherwise canonical path components.

The environment installed at `/etc/weatheredge.env` is based on
`trading/deploy/aws/sfo-weather.env.example`. Apple WeatherKit remains
safe-off until an operator configures a WeatherKit Service ID and private key,
then explicitly sets `ENABLE_APPLE_WEATHER=1`. Never place the `.p8` inside the
source tree; the service rejects keys that are not mode 0600.
The dormant live-risk policy scales its pilot, daily, and per-order limits from
`SFO_LIVE_RISK_CAPITAL`. Established hosts migrate only the historical exact
$50/$20/$10 defaults; custom absolute overrides are retained.

## Timers

- `sfo-forecaster-refresh.timer`: twice hourly from 05:10 through 18:40 PT and
  hourly overnight; refreshes NWS truth, Google Weather within budget, NWP/EMOS
  forecast state for all fifteen cities, and no public artifacts.
- `weatheredge-google-nonsfo-refresh.timer`: one daily budgeted Google runtime
  refresh for the fourteen non-SFO stations.
- `weatheredge-apple-refresh.timer`: four fixed UTC vintages/day for all fifteen
  stations. It exits without a request by default, retains only provider-valid
  temporary data in `/run/weatheredge`, and has zero trading weight.
- `weatheredge-apple-purge.timer`: every ten minutes, offset from Google;
  physically removes expired or unverifiable Apple values even when refresh is
  disabled or unavailable.
- `weatheredge-google-runtime-purge.timer`: every ten minutes; physically
  expires Google runtime rows independently of Apple.
- `sfo-operational-publish.timer`: every ten minutes, offset to :02/:12/:22/:32/:42/:52;
  builds and validates the
  operational JSON snapshot, then publishes it.
- `sfo-strategy-lab-refresh.timer`: fixed wall-clock five-minute cadence;
  bounded research-only build and publish, with no paid Google refresh or
  full-journal rescore. The recurring wrapper forces bounded mode even if
  `/etc/weatheredge.env` says otherwise. Calendar scheduling avoids
  completion-time drift.
- `sfo-dataset-backfill.timer`: nightly at 10:01 UTC (03:01 PDT / 02:01 PST);
  compact source refresh, CLI settlement truth, NWP leads 1 and 2, and
  rolling-origin EMOS. The fixed UTC window starts after retention prune's
  worst-case deadline. Missed heavy-maintenance windows do not replay during
  timer restoration; the next nightly window is used. Lead 3 is manual research.
- `sfo-kalshi-paper-scan.timer`: every five minutes across all configured city
  prediction markets.
- `sfo-kalshi-paper-monitor.timer`: every two minutes; monitors paper exits and
  maker-limit proxy fills.
- `sfo-kalshi-paper-settle.timer`: finality-gated, series-scoped settlement.
- `sfo-kalshi-paper-prune.timer`: archive, upload, verify, and FK-check. Its
  interim default is archive-only, so a missed run waits for the next nightly
  window and the scheduled unit never deletes from the live journal.
- `sfo-forecast-freshness.timer`: publication and forecast health checks.
- `sfo-scheduler-health.timer`: offset five-minute independent scheduler check.
  It requires the thirteen application timers to be enabled and active, verifies
  canonical systemd units, forecast DB safety, artifact checksums and source
  provenance, and local/public Strategy and operational freshness. It repairs
  only age-only failures by starting Strategy Lab and/or operational
  publication under a single-flight lock and 15-minute cooldown. It never
  starts scan, monitor, settlement, or any trading action.

The operational publication path holds `SFO_ARTIFACT_GENERATION_LOCK` while
building, releases it while the prior Pages branch head finishes deployment,
then reacquires it to validate and copy a coherent snapshot. Strategy Lab
builds in staging and takes that lock only for atomic artifact/cache promotion
and manifest rebuild. `SFO_PAGES_LOCK` serializes the delivery gate and Git
update so a newer push cannot cancel an in-flight Pages deployment. The
publisher retries bounded non-fast-forward failures with
`SFO_PAGES_PUSH_ATTEMPTS`.
Strategy lock acquisition is capped at 30 seconds; operational artifact and
Pages lock acquisition are capped at 60 seconds each, leaving service-deadline
headroom for generation and network publication.

The full sync creates the root-owned
`/run/weatheredge-deploy-maintenance` marker after timer-state capture and
before quiescence. Scheduler repair skips while the marker exists. After the
captured timer policy and post-analysis publication are restored, the deploy
removes the marker and runs the scheduler-health service once. A failed deploy
can intentionally leave the marker in place; investigate and complete or roll
back that deployment before removing it.

## Archive, Finality, And Health

The journal archive defaults to
`/opt/weatheredge/trading/data/archive`. Its `manifest.db` records row count,
SHA-256, exact ID coverage, and decision-to-context references for each
compressed daily partition. `run_archive_then_prune.sh` performs lossless
export, feature rollup, optional S3 upload, exact-ID/reference gate, explicit FK
audit, and upload-backed local cleanup in that order. Scheduled runs default to
`SFO_PRUNE_MODE=archive-only`: they log a degraded warning, exit successfully,
and never enter the write-heavy prune. A failed archive or gate still fails the
unit.

Archive-only mode deliberately trades write availability for live-journal
growth. The disk watchdog remains active and fails at its configured ceiling
(85% by default), but it does not delete data automatically. Plan a quiesced
maintenance window before reaching that ceiling. The exact opt-in is
`SFO_PRUNE_MODE=quiesced-delete`; stop every paper-journal writer first and
restore archive-only before timers resume. Deletion creates reusable SQLite
pages but does not shrink the database file; use the separately quiesced
`compact_paper_db.sh` workflow when filesystem space must be reclaimed.

S3 is safe-off until `SFO_ARCHIVE_S3_BUCKET` is configured; the related
variables are `SFO_ARCHIVE_S3_PREFIX`, `SFO_ARCHIVE_AWS_CLI`, and
`SFO_ARCHIVE_KEEP_DAYS`.

Deployment is stricter than scheduled archive/prune: `sync_to_box.sh` fails its
read-only preflight before stopping services unless the bucket and instance
role are available. The bucket must have public access blocked, versioning and
default encryption enabled, and a lifecycle rule for both `paper_trading/` and
`database-snapshots/`. The instance role needs bucket listing plus object
put/get access limited to those two prefixes. After preflight, the deploy gate
uploads a full SQLite snapshot and checksum, downloads the snapshot to a
temporary path, verifies byte equality by SHA-256, and passes full
`integrity_check` and `foreign_key_check` on that downloaded copy before any
source transfer. There is one full structural proof on the recoverable copy;
repeating it before upload adds no proof after verified byte equality. A failed
check never promotes a snapshot or permits deployment. Sanitized phase messages
go to stderr so operators can follow progress through the long read.

The source revision, main branch and clean checkout are rechecked after backup
and after source transfer. Stop concurrent source edits for deployment; these
checks detect persistent changes but are not an immutable source export.

Useful checks:

```bash
systemctl list-timers 'sfo-*' --all
sudo systemctl --failed
cd /opt/weatheredge/trading
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-archive --archive-dir data/archive --check-gate
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-check-foreign-keys --limit 100
sudo systemctl start sfo-operational-publish.service
sudo systemctl start sfo-strategy-lab-refresh.service
```

For an existing large journal, keep paper scan and monitor services paused and run
`/opt/weatheredge/trading/deploy/aws/create_decision_snapshot_index.sh`; resume
the services only after the index build succeeds. The script exits immediately
when all definitions are already current and applies its conservative disk-space
gate only when an index must be built or repaired. It also creates the narrow
pending-admission index that prevents research scans from repeatedly reading the
entire decision journal.

`sync_to_box.sh` attempts `refresh_strategy_analysis_cache.sh` while deployment
maintenance still holds the producers. The job reads the
integrity-checked deploy snapshot rather than the live journal and performs the
expensive historical rescore in a stable transient systemd unit with a 1.6 GB
hard memory cap, low I/O priority, and a fixed runtime deadline. It requires
clean build provenance, validates its source SHA and effective strategy
configuration, and atomically promotes
`strategy_analysis_cache.json` plus its full private replay-evidence artifact.
After promotion, deployment pauses and drains both manifest writers, rebuilds
the bounded public Strategy artifact from the new cache, publishes it, waits
for that exact immutable manifest on Pages, and restores both recurring timers.
Before any timer is restored, the deploy also rejects canonical units with
runtime drop-ins, transient shadows, stale fragments, or an unexpected Strategy
timeout. The stable analysis unit name is stopped and reset on
failure, including a best-effort caller-side cleanup after a dropped transport.
The verified local snapshot is removed after this last consumer, before timers
and disk/freshness checks resume. Because this cache is diagnostic, a failed or
resource-killed rescore is reported and restoration continues; the public
artifact explicitly marks historical analysis deferred.
Recurring Strategy Lab refreshes reuse that validated input while recomputing
current account state, calibration, readiness, and a hard-bounded tail of
indexed scan contexts for live and research-profile candidates. Historical
replay, scorecards, shadow analysis, and decision rollups are cached or marked
deferred; the recurring path never recomputes them, rebuilds missing dataset
research, or scans the full decision journal. An
operator can run the same helper manually by setting
`SFO_STRATEGY_ANALYSIS_DB_PATH` to a verified immutable snapshot. It loads the
root-owned `/etc/weatheredge.env` through passwordless `sudo` before forcing
full-analysis mode and refuses to run against the live paper database.

The freshness watchdog defaults to local operational artifacts no older than
10 minutes, public publication no older than 20 minutes, and Strategy Lab
research no older than 20 minutes. Its public
manifest request uses a unique query key so the GitHub Pages ten-minute CDN TTL
cannot conceal a stalled publisher. Set
`SFO_PUBLICATION_MANIFEST_URL` to the public manifest URL to validate the exact
snapshot visitors receive. A full installer run migrates only the former
15-minute local and 20-minute public default values in `/etc/weatheredge.env`;
operator-customized thresholds are preserved. The guarded environment migrator
also enables the exact former `false` same-day paper-model heartbeat default and
adds the missing research-only `0.05` take-profit margin. Explicit custom values
and duplicate assignments are preserved rather than guessed at deployment time.

For operator-only archive restoration, stop paper services, restore into a new
database, and run the FK audit before any swap. The tested Python API is
`sfo_kalshi_quant.archive.restore_archive_days(archive_dir, db_path, days=...)`;
it verifies hashes and inserts FK parents before children.

## Security Group And Recovery

Allow SSH (`tcp/22`) only from the operator's current public IP. Do not open SSH
to `0.0.0.0/0`. Before host firewall or SSH changes:

1. Confirm an EC2 console or AWS Systems Manager recovery path.
2. Keep a second verified SSH session open.
3. Record the current security-group rule and a recent volume snapshot.
4. Run `sudo systemctl --failed`, `df -h /`, `free -h`, and `ss -tulpn`.

Then change one layer at a time and verify SSH before closing the recovery
session. Production deployment and security-group changes are operator actions,
not part of local verification.

## Platform History

WeatherEdge ran on a 1 GB AWS Lightsail instance until 2026-07-10. That host and
its old env names are retired; deploy scripts accept the old IP/key variable
names, and the forwarding-only sync wrapper, only as temporary compatibility
during EC2 migration.
