# AWS Deployment (EC2)

WeatherEdge production runs on an AWS EC2 `t4g.medium` in `us-east-1`. The host
is Ubuntu 24.04 arm64 (`ubuntu`), and the application root is
`/opt/weatheredge`. Live databases, caches, and dashboard artifacts on that host
are authoritative after sync and refresh.

The operator connection settings belong in the ignored `.local/ec2.env`:

```bash
EC2_IP=replace_with_public_ip
EC2_KEY=/absolute/path/to/deploy-key.pem
REMOTE_USER=ubuntu
```

Keep the key mode at `0600`. Never commit the env file or key.

## Deploy And Install

From the repository root:

```bash
bash trading/deploy/aws/sync_to_box.sh
source .local/ec2.env
ssh -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP"
cd /opt/weatheredge/trading
bash deploy/aws/install_systemd_notimers.sh
```

Both installers first read the host timezone without mutating it. The regular
installer proceeds only when it is already `America/Los_Angeles`; otherwise it
refuses and directs the operator to the cutover-safe timerless installer.
`install_systemd_notimers.sh` quiesces every existing WeatherEdge timer and
paired service before changing a mismatched timezone, then renders every unit
while enabling none. A preflight failure changes nothing; a timezone-set
failure propagates only after services are safely quiesced. Inspect
`/etc/weatheredge.env`, start each service manually, and only then enable the
approved timers.

The full sync does not use `--delete`. The scheduled
`sync_forecaster_source.sh` does, but both use
`forecaster-runtime.rsync-filter`, which preserves runtime databases, caches,
their SQLite `-wal`/`-shm` sidecars, generated publication JSON,
`STALE_FORECAST`, and `models/`. The committed `forecast_data.json` and
`weather_story_data.json` inputs are deployed by both sync paths; they are
source-controlled inputs, unlike their runtime-produced JSON siblings.
The full sync also deploys the root `pyproject.toml` and `README.md`; both
installers keep the executable environment under `trading/.venv` while running
the editable install from `/opt/weatheredge`. The scheduled forecaster-only sync
does not reinstall that environment. After every full transfer succeeds, the
sync removes only the obsolete `trading/pyproject.toml` and the eleven audited
top-level forecaster scripts now housed under `forecaster/research/`. No runtime
database, raw input, model directory, or publication artifact is part of that
cleanup.

During an upgrade, the installer uninstalls the retired `sfo-kalshi-quant`
distribution before installing `weatheredge`, then verifies that the old
distribution is absent and the `sfo-kalshi` console entry belongs to
`weatheredge`. It removes only the old generated
`trading/sfo_kalshi_quant.egg-info` metadata that pip's legacy editable
uninstall leaves behind and the transient `trading/weatheredge.egg-info`
created during the replacement build. Verification requires exactly one
WeatherEdge distribution metadata record and one correctly owned console
entry. A surviving nested trading manifest is a hard preflight failure.

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
`trading/deploy/aws/sfo-weather.env.example`.

## Timers

- `sfo-forecaster-refresh.timer`: twice hourly from 05:10 through 18:40 PT and
  hourly overnight; refreshes NWS truth, Google Weather within budget, NWP/EMOS
  forecast state for all fifteen cities, and no public artifacts.
- `sfo-operational-publish.timer`: every five minutes; builds and validates the
  operational JSON snapshot, then publishes it.
- `sfo-strategy-lab-refresh.timer`: every fifteen minutes; research-only build
  and publish, with no paid Google refresh.
- `sfo-dataset-backfill.timer`: nightly; compact source refresh, CLI settlement
  truth, NWP leads 1 and 2, and rolling-origin EMOS. Lead 3 is manual research.
- `sfo-kalshi-paper-scan.timer`: every five minutes across all configured city
  prediction markets.
- `sfo-kalshi-paper-monitor.timer`: every two minutes; monitors paper exits and
  maker-limit proxy fills.
- `sfo-kalshi-paper-settle.timer`: finality-gated, series-scoped settlement.
- `sfo-kalshi-paper-prune.timer`: archive, verify, FK-check, then prune.
- `sfo-forecast-freshness.timer`: publication and forecast health checks.

Both publication paths hold `SFO_ARTIFACT_GENERATION_LOCK` while building,
validating, and copying. `SFO_PAGES_LOCK` separately serializes Git updates.
The publisher retries bounded non-fast-forward failures with
`SFO_PAGES_PUSH_ATTEMPTS`.

## Archive, Finality, And Health

The journal archive defaults to
`/opt/weatheredge/trading/data/archive`. Its `manifest.db` records row count,
SHA-256, exact ID coverage, and decision-to-context references for each
compressed daily partition. `run_archive_then_prune.sh` performs lossless
export, feature rollup, optional S3 upload, exact-ID/reference gate, explicit FK
audit, prune, and upload-backed local cleanup in that order. A failed archive or
gate prevents deletion.

S3 is safe-off until `SFO_ARCHIVE_S3_BUCKET` is configured; the related
variables are `SFO_ARCHIVE_S3_PREFIX`, `SFO_ARCHIVE_AWS_CLI`, and
`SFO_ARCHIVE_KEEP_DAYS`.

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
`/opt/weatheredge/trading/deploy/aws/create_decision_snapshot_index.sh` once;
resume the services only after the index build succeeds.

The freshness watchdog requires operational artifacts no older than 10 minutes
and Strategy Lab research no older than 20 minutes. Set
`SFO_PUBLICATION_MANIFEST_URL` to the public manifest URL to validate the exact
snapshot visitors receive.

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
