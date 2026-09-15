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
  intact. The optional historical Strategy Lab rescore is memory-capped in a
  transient systemd unit; failure leaves historical panels explicitly deferred
  and does not prevent timer restoration.
- `sync_to_lightsail.sh` is a deprecated forwarding-only compatibility wrapper
  for the EC2 migration window. New commands must use `sync_to_box.sh`.
- `pull_paper_db.sh` allocates a private mode-700 directory on the remote host,
  writes and verifies a mode-600 SQLite backup there, verifies the downloaded
  copy, and removes the complete temporary directory before publishing the
  local database atomically.
- `sync_forecaster_source.sh` is a disabled compatibility tombstone. A
  forecaster-only refresh could split production across two revisions while
  leaving `build_info.json` and the analysis cache falsely source-matched.
  Normal changes and recovery both use the clean-main full deploy so root
  packaging inputs, both source trees, dependencies, units, provenance, and
  published artifacts identify one exact revision.
- Runtime DBs and their SQLite `-wal`/`-shm` sidecars, publication JSONs,
  `STALE_FORECAST`, and `models/` are never clobbered. The tracked
  `forecast_data.json` and `weather_story_data.json` inputs are intentionally
  copied by the full deploy.
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
are available. After quiescing, it creates a consistent SQLite backup, hashes and
uploads it with server-side encryption, downloads it to a temporary restore
path, verifies its checksum, and runs full integrity and foreign-key checks on
that downloaded copy. The same-byte pre-upload scan is redundant and omitted.
Only then does the
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

The forecaster runtime installs only `certifi`, `numpy`, `pandas`, and the
PyJWT/cryptography ES256 signing stack; the correctly formed command is a
hash-verified install from `requirements/production.lock`. Heavy training
dependencies do not belong on the production box.

## Cadence And Responsibilities

- Forecast refresh: twice hourly from 05:10 through 18:40 PT and hourly
  overnight; all twenty cities, SFO flagship.
- Apple WeatherKit research refresh: four fixed UTC vintages/day, one bundled
  hourly+daily request per city. It is disabled by default, temporary-only,
  and has zero live trading weight. See `docs/APPLE-WEATHERKIT.md`.
- Provider runtime purge: every ten minutes, independent of refresh success.
- Operational publication: every ten minutes, offset to :02/:12/:22/:32/:42/:52; builds
  `trading_signal.json`, `cities_data.json`, and `publication_manifest.json`.
- Strategy Lab publication: fixed wall-clock five-minute cadence; bounded
  research-only artifact build backed by a separately timestamped
  historical-analysis cache. A full
  source/config-bound cache refresh runs at low I/O priority during deployment
  maintenance. It reads the verified immutable deploy snapshot rather than the
  live journal. The snapshot is removed before producers and health checks
  resume, avoiding transient disk-pressure failures during restoration.
- Paper scan: every five minutes, all configured cities, two profiles
  (`PAPER_RISK_PROFILES=live,research`), reservation-first with guarded
  profile-scoped taker crosses under `PAPER_ENTRY_MODE=limit`, and
  `PAPER_CITIES=all`.
- Paper monitor: every two minutes.
- Dataset/backfill: nightly, including NWP leads 1 and 2. Lead 3 is manual.

Publication is finality-aware and race-safe: builders share
`SFO_ARTIFACT_GENERATION_LOCK`; the publisher serializes the Pages delivery
gate with `SFO_PAGES_LOCK`, then reacquires the artifact lock to validate and
copy the exact snapshot before pushing. A short prior-branch delivery wait
reduces churn; by default its first timeout permits a successor push to replace
a stuck deployment. Strategy promotion defaults to a 30-second artifact-lock
wait; operational artifact and Pages locks default to 60 seconds each. The
prior-branch wait defaults to 60 seconds, distinct from the longer exact-manifest
verification used by deployment and scheduler recovery. These
bounds leave generation and push headroom inside their systemd deadlines.
Both publisher and paper-scan locks default under `/opt/weatheredge/.locks` so
reboots clean temporary storage without weakening overlap protection. Configure
the deploy key as `/home/ubuntu/.ssh/sfo_weather_pages_deploy` and the Git source
as `git@github.com:Jaxsonb04/weather_edge.git`.

`sfo-scheduler-health.timer` runs on an offset five-minute wall clock. Its
root-owned helper verifies that the twelve application timers are enabled and
active, rejects effective unit drift, stale/missing forecast state, checksum or
source-provenance mismatches, and validates both local and public artifact
freshness. Only age-only failures are eligible for repair: it may start the
bounded Strategy Lab service and/or the operational publisher under a
single-flight lock and 15-minute cooldown. It never starts paper scan, monitor,
or settlement services and never changes placement or live-trading flags.

`sync_to_box.sh` creates `/run/weatheredge-deploy-maintenance` immediately
before quiescence. Scheduler checks skip while that root-owned marker exists;
the deploy restores its captured timer policy, removes the marker, and runs one
explicit scheduler-health check last. A deployment that fails before any source is
transferred restores every timer it captured, retired ones included because the
old release is still running, and releases the marker itself; one that fails
after the transfer starts leaves the marker intentionally present so
a partially installed tree is not auto-repaired (see Release Deploy And
Rollback).

## Release Deploy And Rollback

The ordered operator runbook for the single deploy of the 2026-09-13
audit-remediation release (`behavior-v3-forecast-and-execution-2026-09-07`,
`research-entry-risk-v3-scaling-2026-09-13`). Production runs v2, source
`2a6432e3b`. Phase 0 and the recovery variables apply to any deploy.

Recovery variables read by `sync_to_box.sh`:

| Variable | Use |
|---|---|
| `SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1` | Stranded host (maintenance marker left over, or the database exists and no timer is enabled). A successful deploy restores the release canonical timer set -- what `install_systemd.sh` enables, minus retired timers, plus the scheduler watchdog, 13 timers -- and prints the list before anything is quiesced. |
| `SFO_DEPLOY_KEEP_CAPTURED_TIMERS=1` | A host deliberately paused by an operator: deploy and keep exactly the captured timers. Never together with the variable above. |
| `WEATHEREDGE_RECOVERY_SSH_ATTEMPTS`, `WEATHEREDGE_RECOVERY_SSH_RETRY_SECONDS` | Retries for the pre-transfer recovery when SSH itself fails (defaults 4 and 30 s). |

Without either override a stranded host is refused before anything is quiesced.
The backup preflight has already run by then, so its sweep of aged and
interrupted local snapshots (step 0.4) may have deleted files.
A healthy host needs neither.

### Phase 0: confirm the host state (read-only)

0.1 Prove SSH from the deploy machine over the network you will deploy on.
Opening the security group, Tailscale or SSM is the owner's action. Use a
stable link: the host is quiesced for roughly 30-45 minutes.

0.2 On the box:

```bash
test ! -e /run/weatheredge-deploy-maintenance && echo "no maintenance marker"
systemctl list-unit-files 'sfo-*.timer' 'weatheredge-*.timer' --state=enabled --no-legend | wc -l   # 14 on v2
cat /opt/weatheredge/forecaster/build_info.json                                          # v2: 2a6432e3b
find /opt/weatheredge/trading/sfo_kalshi_quant /opt/weatheredge/forecaster -name '*.py' \
  -newer /opt/weatheredge/forecaster/build_info.json | head                              # expect nothing
pgrep -af 'sync_to_box|backup_paper_db|sqlite3 .*backup' || echo "no deploy or backup running"
systemctl --failed
df -P /opt/weatheredge
ls -l /opt/weatheredge/trading/data/paper_trading.db
ls -la --time-style=full-iso /opt/weatheredge/trading/data/backups/
sudo grep -nE '^(SFO_PRUNE_MODE|SFO_DATABASE_BACKUP_KEEP_DAYS|PAPER_ENTRY_MODE|SFO_FRESHNESS_ALERT_URL)=|weatheredge-migration' /etc/weatheredge.env
sudo journalctl -u weatheredge-google-nonsfo-refresh -n 30 -o cat
curl -s https://jaxsonb04.github.io/weather_edge/publication_manifest.json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["published_at"])'
```

0.3 Classify the host:

- **Healthy v2:** no marker, 14 enabled timers, a public manifest under 20
  minutes old. Deploy with no recovery variable.
- **Stranded:** a marker, or 0 enabled timers while the database exists. The
  deploy refuses on its own. If `build_info.json` still says `2a6432e3b` and
  no `.py` file is newer (the earlier deploy died before its first rsync),
  you may restore it by hand and then treat it as healthy: from the Mac,
  `ssh ... bash -s restore <timers> < trading/deploy/aws/disable_systemd_timers.sh`
  with producers first and `sfo-scheduler-health.timer` last, then
  `sudo rm -f /run/weatheredge-deploy-maintenance`. Otherwise deploy with
  `SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1`.
- **Deliberately paused:** `SFO_DEPLOY_KEEP_CAPTURED_TIMERS=1`.

A leftover marker also stops the backup preflight from reclaiming what an
interrupted backup left (`.restore-check.*` directories and unhashed
snapshots older than six hours). After `pgrep` shows no deploy or backup
running, remove a stale marker before deploying; an empty timer capture still
forces the canonical override.

0.4 Disk. The preflight needs free space of at least the database size plus
1 GiB after its sweep. The sweep removes snapshot and checksum pairs older
than `SFO_DATABASE_BACKUP_KEEP_DAYS` (default 1, which `find` rounds to about
48 hours). A younger hashed snapshot is never removed automatically: wait for
it to age out -- the stranded `paper_trading-20260913T013049Z.sqlite3` is swept
from about 2026-09-15T01:31Z -- or delete it by hand after confirming
`database-snapshots/` in S3 holds it. Starting too early fails the preflight
safely, before anything is quiesced. The runbook staged on the Mac at
`/tmp/deploy_round3.sh` is retired: its free-space step ignores the sweep and
its timer count includes the disabled Apple refresh timer.

### Phase 1: source

1.1 Push `integration/audit-round2-rebased`, open the PR to `main`, wait for
`verify.yml` to pass, and merge.

1.2 In `/Users/jaxson/develop/WeatherEdge`: `git checkout main && git pull --ff-only`.
HEAD must be the merge commit, `git status --porcelain` must be empty, and
`.local/ec2.env` must hold `EC2_IP` and `EC2_KEY`.

### Phase 2: box environment, before deploying

2.1 `sudo cp -p /etc/weatheredge.env /etc/weatheredge.env.pre-v3`

2.2 Set `SFO_FRESHNESS_ALERT_URL` (OPS-3). Without it no `OnFailure` alert is
delivered: a failed prune, or any other failing unit, reaches only the journal.
The webhook does not cover the exchange settlement `MISMATCH` verdicts or the
Google client-error circuit breaker. Neither fails its unit (the settle command
exits 0 regardless, and the Google refresh exits on the EMOS baseline's
result), so `OnFailure` never fires for them: they are log-only, with or
without the webhook. Step 5.7 gives the journal checks.

2.3 Confirm `PAPER_ENTRY_MODE=limit`. Leave
`GOOGLE_WEATHER_CLIENT_ERROR_BREAKER`, `SFO_EXCHANGE_SETTLEMENT_CHECK` and
`SFO_PAGES_HISTORY_MAX_COMMITS` unset; their defaults are 12, on and 1500. No
key is renamed.

2.4 Do not add `SFO_PRUNE_MODE` by hand. The installer's env migration appends
`SFO_PRUNE_MODE=archive-only`, under a comment, to a file that lacks the key,
and production's file lacks it. Nightly deletion stays off until step 5.5.

### Phase 3: deploy

3.1 Timing: avoid 07:50-09:30 UTC (prune) and 09:50-10:50 UTC (dataset
backfill, now allowed 45 minutes), and prefer to avoid 13:55-14:05 UTC. Stops
and exits do not run while the host is quiesced.

3.2 From the shared checkout on `main`:

```bash
cd /Users/jaxson/develop/WeatherEdge
nohup caffeinate -dimsu bash trading/deploy/aws/sync_to_box.sh \
  > /tmp/we-deploy-$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 < /dev/null &
```

On a stranded host, prefix the command with
`SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1`.

3.3 Expect, in order: `WEATHEREDGE_DATABASE_PRESENT=1` and
`database backup preflight passed`;
`retired WeatherEdge timer captured; the installed release will not restore it, only a pre-transfer recovery would: weatheredge-apple-refresh.timer`;
a `WEATHEREDGE_BACKUP_SNAPSHOT=` line; installer output including
`notice: appended SFO_PRUNE_MODE=archive-only` and possibly a removed
superseded journald drop-in; `units rendered and installed; all WeatherEdge timers remain disabled`;
and finally `Restored 9 producer timer(s); watchdog restored last=1.` and
`Scheduler watchdog restored after maintenance=1.` With the canonical override
the 13 timers are listed before the host is quiesced.

3.4 If it fails:

- **Before the first rsync** (preflight, capture, quiesce, backup): on an
  established host the deploy restores every timer it captured and releases
  the marker itself, retrying lost SSH connections. That includes the retired
  `weatheredge-apple-refresh.timer`, because v2's scheduler watchdog still
  requires it. Look for `deploy stopped before any source was transferred`
  followed by `Pre-transfer recovery restored N timer(s)` (14 on a healthy v2
  host). If it prints
  `pre-transfer recovery failed`, return to step 0.2. A host that was already
  stranded is left quiesced.
- **After the first rsync** (transfer, install, unit verification, account
  cutover): the host stays quiesced with the marker by design. Fix the cause and
  rerun; the rerun captures no timers, so it needs
  `SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1`. Or roll back (phase 6).
- **After the cutover gate:** the deploy's own recovery trap restores the timers
  or quiesces again.
- In every case, check `data/backups` for a leftover snapshot.

3.5 What the migrations touch: the first `PaperStore` init runs inside the
quiesced window (`validate_account_cutover.py`). It creates three empty tables
(`ladder_bin_outcomes`, `kalshi_market_resolutions`,
`paper_settlement_exchange_checks`) with their indexes and records one
`schema_migrations` row. There is no `ALTER` and no index on
`decision_snapshots` or `paper_orders`, so nothing is rewritten on the ~24 GB
journal. `weather.db`, `production.lock` and `pyproject.toml` are unchanged.

### Phase 4: verify

4.1 `build_info.json` shows the merge commit and `"source_dirty": false`.

4.2 13 enabled timers (the command in 0.2);
`systemctl is-enabled weatheredge-apple-refresh.timer` prints `disabled`; no
marker; `/etc/systemd/journald.conf.d` holds only `zz-weatheredge.conf`.

4.3 `sudo grep -n -B6 '^SFO_PRUNE_MODE=' /etc/weatheredge.env` shows
`archive-only` under the migration comment.

4.4 `sqlite3 'file:/opt/weatheredge/trading/data/paper_trading.db?mode=ro' .tables`
lists the three new tables.

4.5 The forecaster refresh exits 0 and logs `awaiting onboarding backfill` for
`lv`, `min`, `satx`, `nola` and `dc`; the scan logs
`[lv] skipped: calibration unavailable` and the same for the other four.

4.6 Fingerprints, from new rows:
`SELECT risk_profile, strategy_fingerprint, count(*) FROM paper_orders WHERE created_at > '<deploy UTC>' GROUP BY 1, 2`.
Live SFO `e1b9704afa53970f4edbdca3`, live elsewhere
`d3751adbf7e84c6e424d46da`, research `b123f57836129c1df1c995bd` in every city
(all pinned in `test_research_sleeves.py`). Research day-ahead resting orders
expire 30 minutes after creation; live orders keep 15.

4.7 The settle journal has an `exchange settlement check:` line and no
`MISMATCH`.

4.8 The public manifest is under 20 minutes old, and the gh-pages commit
message carries `(source <merge commit>)`.

4.9 `df` is below 85%, and `systemctl --failed` is empty. A dead Google key
does not fail a unit by itself: the Google refresh units exit on the EMOS
baseline's result, so an open breaker shows only in the journal (step 5.7).

### Phase 5: post-deploy data work, in order

Run the forecaster steps as the app user from `/opt/weatheredge/forecaster`,
starting each right after a :10 or :40 refresh finishes (see Adding A City).

5.1 Backfill the five new cities: steps 1 and 2 of the Adding A City block with
`SLUGS=lv,min,satx,nola,dc`.

5.2 One all-city EMOS rebuild. It rewrites the fifteen existing cities' archive
with debiased spreads and onboards the five new ones. Time it: the nightly
chain's limit is 2700 s and no twenty-city run has been measured.

```bash
time .venv/bin/python emos_forecast.py --db weather.db --backfill --lead 1 --cities all
time .venv/bin/python emos_forecast.py --db weather.db --backfill --lead 2 --cities all
```

5.3 Step 4 of the Adding A City block must print `awaiting=0` and exit 0; the
next scan analyses all five cities.

5.4 Exchange settlement backfill, from `/opt/weatheredge/trading`, away from
the :10 and :40 minutes. Repeat while `unchecked` keeps falling. Any
`EXCHANGE SETTLEMENT MISMATCH` is an incident. Never run a wide plain
`paper-resettle --verify`.

```bash
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db \
  paper-resettle --verify --exchange-check-only --exchange-max-fetches 100 --days 100
```

5.5 Supervised catch-up prune, which turns nightly deletion on:

```bash
sudo install -o root -g root -m 600 /dev/null /run/weatheredge-deploy-maintenance
TIMERS="sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer sfo-kalshi-paper-settle.timer sfo-dataset-backfill.timer sfo-strategy-lab-refresh.timer sfo-operational-publish.timer sfo-kalshi-paper-prune.timer sfo-forecast-freshness.timer"
sudo systemctl stop $TIMERS          # stop, do not disable; wait until their services are inactive
systemctl show -p User sfo-kalshi-paper-prune.service   # confirm the app user
sudo sed -i 's/^SFO_PRUNE_MODE=archive-only$/SFO_PRUNE_MODE=quiesced-delete/' /etc/weatheredge.env
sudo systemd-run --unit=weatheredge-catchup-prune --uid=ubuntu \
  -p EnvironmentFile=/etc/weatheredge.env -p WorkingDirectory=/opt/weatheredge/trading \
  -p MemoryMax=3700M /usr/bin/env bash /opt/weatheredge/trading/deploy/aws/run_archive_then_prune.sh
journalctl -fu weatheredge-catchup-prune
sqlite3 /opt/weatheredge/trading/data/paper_trading.db 'ANALYZE;'
# In the same window, decide whether to run compact_paper_db.sh.
sudo sed -i '/^SFO_PRUNE_MODE=/d' /etc/weatheredge.env   # keep the migration comment above it
sudo systemctl start $TIMERS
sudo rm -f /run/weatheredge-deploy-maintenance
sudo systemctl start sfo-scheduler-health.service
```

After the next 08:20 UTC run, read `journalctl -u sfo-kalshi-paper-prune.service`.

5.6 From the Mac shared checkout at the merge commit:
`bash trading/deploy/aws/deploy_web_app.sh`. The next publish serves the new
SPA, including the twenty-city social card.

5.7 Watch for 48 hours:

- the first nightly dataset-backfill wall time against 2700 s:
  `systemctl show -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p Result sfo-dataset-backfill.service`;
- `PAPER_EXPIRED` research rows whose reason starts `stale research quote`,
  each of which holds its market for about one scan tick;
- `canonical research entry rejected` decision rows, concurrent resting research
  quotes, and research zero-room time (the replay expects roughly 20-28% of
  samples with no room);
- the Google breaker, which is log-only (below);
- about ten days in, the first gh-pages re-root (`gh-pages history reached 1500
  publications; re-rooting the branch` in the `sfo-operational-publish`
  journal), after which the public manifest and SPA must still load
  (`SFO_PAGES_HISTORY_MAX_COMMITS=0` disables it).

Two safety signals never alert, whether or not `SFO_FRESHNESS_ALERT_URL` is
set: the exchange settlement `MISMATCH` verdicts and the Google client-error
circuit breaker. `_exchange_settlement_guard` cannot change the settle
command's exit status, and `google_multicity_refresh.py` returns the EMOS
baseline's status even while it prints that the breaker is OPEN, so
`OnFailure=sfo-alert@` never fires for either. They are log-only. Check both by
hand every day, on the box; no output means neither fired:

```bash
sudo journalctl -u sfo-kalshi-paper-settle.service --since yesterday --no-pager \
  | grep -E 'EXCHANGE SETTLEMENT (MISMATCH|CHECK FAILED)|STANDING EXCHANGE SETTLEMENT MISMATCHES'
sudo journalctl -u sfo-forecaster-refresh.service -u weatheredge-google-nonsfo-refresh.service \
  --since yesterday --no-pager | grep 'circuit breaker is OPEN'
```

Until `SFO_FRESHNESS_ALERT_URL` is set, also check the public manifest's age
daily: a stale publication fails the freshness unit, and that failure alerts
only through the webhook.

### Phase 6: rollback

Triggers: a ledger or restatement defect, a unit failing every tick, or an
exchange `MISMATCH` traced to code.

6.1 On the box first: `sudo systemctl enable --now weatheredge-apple-refresh.timer`.
This release disables that timer, v2's `check_scheduler_health.sh` still lists
it as canonical, and v2's `sync_to_box.sh` re-enables it only when the unit is
absent, so without this step the v2 watchdog fails every five minutes.

6.2 Revert the merge on `main` through a PR (`git revert -m 1 <merge commit>`),
then pull the shared checkout clean at the revert commit.

6.3 Repeat step 0.2. v2's `sync_to_box.sh` has neither the stranded-host guard
nor pre-transfer recovery: it must start on a healthy host (no marker, timers
enabled), and on a quiesced host it would restore nothing. Then run the same
`nohup` deploy command, with no recovery variable (v2 ignores them). Its backup
gate still needs the database size plus 1 GiB free.

6.4 Leave the `SFO_PRUNE_MODE=archive-only` line and its comment: v2's prune
wrapper already defaults to archive-only.

6.5 Redeploy the v2 SPA with `deploy_web_app.sh` from the reverted tree.

6.6 Harmless leftovers: the three new tables and their migration row, the
`zz-weatheredge.conf` journald drop-in, `cancel_request` diagnostics on expired
research quotes, and level-2 live fills whose `ask_levels` v2's restatement
ignores (they may show as unverified). Fingerprints return to v2 (live SFO
`88e417a64d8be9b1bb933b3b`, live elsewhere `36be72cf3bf65bb5fb4441f0`,
research `92934c133d00d85deb078b3c`), which resets the live readiness clock a
second time.

6.7 Restore the paper database from the deploy's S3 snapshot under
`database-snapshots/` only for proven ledger corruption: it loses every write
since the deploy. Restore to a new path with paper services stopped and run
`paper-check-foreign-keys` before any swap.

## Adding A City (Post-Deploy Backfill)

A new registry row (`forecaster/cities.py` + `trading/sfo_kalshi_quant/cities.py`,
kept byte-identical) is picked up by every `--cities all` unit on the next tick.
What happens on the box before the backfill below has run, and why nothing
trades:

- **Scanner: fails closed per city.** `SfoForecasterAdapter
  .load_calibration_outcomes` returns the station's scored lead-1 EMOS rows,
  `ResidualCalibrator` refuses fewer than 30 of them, and both `cmd_analyze` and
  `cmd_portfolio_scan` catch that and print `[slug] skipped: calibration
  unavailable (...)` before any target is analysed. The series is *skipped, not
  traded*, in both paper books (`PAPER_CITIES=all`).
- **Live EMOS serve: excluded from health, not red.** `serve_live_emos` needs
  `EMOS_MIN_TRAIN = 60` truth-matched NWP days at the fit lead before it emits
  a row. A station with no `forecast_emos_daily_high` row of any source is
  reported by `emos_forecast.py --serve-rolling` as `awaiting onboarding
  backfill` and left out of its served/targets accounting
  (`emos_forecast.awaiting_onboarding`), so `sfo-forecaster-refresh` keeps
  exiting 0 and `OnFailure=sfo-alert@` does not page 38x/day for the other
  cities' healthy rows. A station that *has* served or been scored before and
  is now below the floor is still an outage and still fails the unit.
- **Forecast health / status alerts: one `info` per new station.**
  `forecast_health.py` publishes `emos-live-onboarding` (level `info`, not
  `warning`) for a station with no EMOS row of any source; `clisfo-stale`
  ("CLI truth missing for station") appears once and clears on the first
  `city_truth.py --refresh --cities all` tick of the refresh unit. Both are
  expected transient state after a registry change, not an outage.
- **Nightly `sfo-dataset-backfill` alone is too slow.** Its
  `city_truth.py --backfill-iem --start-year 2026` fills the truth side on the
  first night, but `nwp_archive.py --daily` only reaches back five days, so the
  60-day serve floor takes about two months and the scanner's 30 *scored*
  lead-1 rows (rolling-origin rows only start once 60 prior days exist) about
  three. Run the deep backfill once instead.

After deploying a registry change, run the backfill detached from the
forecaster directory with its venv (`__FORECASTER_DIR__` in the unit files;
`SLUGS` is the comma list of new slugs, e.g. `lv,min,satx,nola,dc` for the
2026-09-13 expansion). It only touches the new stations' rows, so it can run
while the timers stay enabled, but it writes `weather.db` beside the 30-minute
`sfo-forecaster-refresh` and the 10:01 UTC `sfo-dataset-backfill`. Start each
step right after a :10 or :40 refresh has finished
(`systemctl is-active sfo-forecaster-refresh.service` prints `inactive`), never
inside the 10:01-10:46 UTC dataset window, and keep it off the top-of-hour
deploy gate window. `emos_forecast.py` waits up to 30 s on a locked database
instead of sqlite3's 5 s default, so a short overlap stalls rather than failing
a refresh or a backfill step. Open-Meteo previous-runs depth was
verified at 420 days for all eight models at KLAS/KMSP/KDCA/KSAT/KMSY on
2026-09-13.

```bash
cd /opt/weatheredge/forecaster
SLUGS=lv,min,satx,nola,dc
# 1. NWP previous-runs archive, 400+ days deep (one request per model per
#    300-day chunk per city; Open-Meteo previous-runs depth, not the daily
#    5-day window). --end is yesterday in the station's climate day.
.venv/bin/python nwp_archive.py --db weather.db --backfill --cities "$SLUGS" \
    --start "$(date -d '-420 days' +%F)" --end "$(date -d '-1 day' +%F)"
# 2. Settlement truth from the IEM CLI archive (default --start-year is two
#    calendar years back; the nightly timer only refreshes the current year).
.venv/bin/python city_truth.py --db weather.db --backfill-iem --cities "$SLUGS"
# 3. Scored rolling-origin EMOS rows at the two served leads. This is what
#    marks the station onboarded (first forecast_emos_daily_high rows).
.venv/bin/python emos_forecast.py --db weather.db --backfill --lead 1 --cities "$SLUGS"
.venv/bin/python emos_forecast.py --db weather.db --backfill --lead 2 --cities "$SLUGS"
# 4. Confirm the floors are cleared before expecting the city in a scan.
.venv/bin/python city_truth.py --db weather.db --coverage --cities "$SLUGS"
.venv/bin/python emos_forecast.py --db weather.db --serve-rolling --cities "$SLUGS"
```

Step 4's serve must print `served=N targets=N ... awaiting=0` for the new slugs
and exit 0. Then watch one paper-scan cycle: the new slugs must move from
`skipped: calibration unavailable` to a normal per-target analysis, and the
`emos-live-onboarding` notices disappear from the published forecast health.
No strategy fingerprint changes (the registry is not part of the config hash),
but both books' opportunity sets grow, so the research and live ledgers gain
new series from the first traded day onward. Research-side note: the
`REGION_BY_SERIES` entry a new city gets also places its station in the
research climate-region pooling cohort (`research_candidates.py`), so once it
has scored rows it contributes to the pooled calibration of the existing
cities in that region (DC joins NYC/BOS/PHL in `northeast`, SATX joins
DAL/AUS/HOU in `texas`, and so on).

Not automatic: `weatheredge-google-nonsfo-refresh.service.in` carries a static
`--cities` list bounded by the 260 events/day Google cap. It stays at the
fourteen original non-SFO cities (19 x 4 + 190 = 266/day would breach the
cap); `test_google_nonsfo_refresh_unit_covers_every_configured_non_sfo_city_once_daily`
lists the excluded slugs. Google Weather is research corroboration only, so
the new cities trade on the NWP -> EMOS -> CLI path without it.

## Archive-Gated Retention

`sfo-kalshi-paper-prune.timer` runs `run_archive_then_prune.sh`, which:

1. Exports every complete UTC day into the archive directory.
2. Builds the derived feature store (non-fatal and rebuildable).
3. Uploads to S3 only when configured.
4. Requires the manifest's exact-ID and context-reference coverage gate.
5. Runs `paper-check-foreign-keys`.
6. Deletes from the live journal in bounded batches
   (`SFO_PRUNE_MODE=bounded-delete`, the default), gated on step 4 having
   passed: an `archive_gate_passed` flag set only by the gate's own success is
   re-checked before any delete, so no reordering or future edit can put a
   delete ahead of the archive that makes it recoverable.
7. Removes old local partitions only after verified upload.

Each delete batch commits and releases SQLite's write lock, and the scan and
monitor wait on a 30 s `busy_timeout`. `SFO_PRUNE_MAX_BATCH_SECONDS` (2 s) is a
shrink target measured *after* each batch, not a ceiling: a batch runs to
completion at the current row limit and only an overrun halves the limit for the
next one (floor 500), so the first batch of a run can exceed it. Step 7's archive
cleanup runs whether or not the delete succeeded, and removes uploaded partition
files, not rows or free pages; a failed delete is reported after it and still
fails the unit.

`SFO_PRUNE_MODE=quiesced-delete` runs the same delete plus an explicit operator
assertion that the paper scan, monitor, settlement, dataset, and other journal
writers are stopped; use it for a supervised catch-up, not on the timer.
`SFO_PRUNE_MODE=archive-only` is the escape hatch: it exits successfully without
touching the live journal, which does **not** bound growth -- roughly 0.7 GB/day,
the condition that made the deploy backup gate unsatisfiable in September 2026.
The disk watchdog is only a last-resort alarm at the configured ceiling (85% by
default) and never deletes anything.

The prune makes old pages reusable inside SQLite; reclaiming filesystem space
still requires the separately quiesced `compact_paper_db.sh` workflow.

The default archive is `/opt/weatheredge/trading/data/archive`; the manifest is
`manifest.db`. Configure `SFO_ARCHIVE_DIR`, `SFO_ARCHIVE_KEEP_DAYS`,
`SFO_ARCHIVE_S3_BUCKET`, `SFO_ARCHIVE_S3_PREFIX`, and `SFO_ARCHIVE_AWS_CLI`.
Full-database deployment backups use the same bucket with
`SFO_DATABASE_BACKUP_S3_PREFIX`; local verified copies are retained according
to `SFO_DATABASE_BACKUP_KEEP_DAYS` (one day by default; the verified S3 tier
retains database snapshots for 35 days).

Preflight also checks peak local capacity before timers are quiesced. The volume
must have room for one full SQLite snapshot plus 1 GiB of operating headroom.
The uploaded local copy is removed before the restore copy is downloaded;
clean only old, independently verified local
snapshots if this gate refuses a deploy.
Without a bucket, the local ring buffer remains authoritative and cleanup skips
unuploaded files.

Health and finality checks:

```bash
cd /opt/weatheredge/trading
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-archive --archive-dir data/archive --check-gate
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-check-foreign-keys --limit 100
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db paper-resettle --verify --days 14
```

`paper-resettle --verify` also reconciles each settled lot against the
exchange's own finalized result. An `EXCHANGE SETTLEMENT MISMATCH` or
`STANDING EXCHANGE SETTLEMENT MISMATCHES` line on stderr is an incident (see
`docs/SETTLEMENT-OBSERVABILITY.md`, section 3). Neither line fails the settle
unit, so neither alerts, even with `SFO_FRESHNESS_ALERT_URL` set; the daily
journal check is in Release Deploy And Rollback, step 5.7.

To backfill exchange verdicts for older lots, add `--exchange-check-only` and
widen `--days`. That leaves alone the CLI verification rows that restatement
reads. Setting `SFO_EXCHANGE_SETTLEMENT_CHECK=off` in the EnvironmentFile turns
the check off on the settle timer without editing the unit.

For an existing large journal, keep paper scan and monitor services paused and
run `create_decision_snapshot_index.sh` once before resuming them. It builds the
covering decision-report index without putting that expensive migration on
normal service startup.

Restore only to a new DB while paper services are stopped, using the tested
`restore_archive_days` API, then run `paper-check-foreign-keys` before any swap.

## Publication Health

Set
`SFO_PUBLICATION_MANIFEST_URL=https://jaxsonb04.github.io/weather_edge/publication_manifest.json`.
The default watchdog rejects local operational artifacts older than 10 minutes,
public publication or Strategy Lab research older than 20 minutes, disk
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
Future-live loss limits are expressed as percentages of
`SFO_LIVE_RISK_CAPITAL`; the guarded installer migration replaces only the
historical exact $50/$20/$10 defaults and preserves any custom operator values.

See [`../../../docs/aws_deployment.md`](../../../docs/aws_deployment.md) for host
details, security-group policy, and operator recovery.
