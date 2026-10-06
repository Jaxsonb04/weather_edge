# V7 Mac research worker

The Mac can handle offline audits, calibration comparisons, model training and
restore verification. AWS remains responsible for continuous collection, the
paper ledger and dashboard publication. A sleeping or disconnected Mac must not
stop those services or change a trading policy.

At the user's subsequent request, future local research runs are disabled pending
plan approval. Any new compute delegation or major EC2 change requires an
explicitly reviewed plan before implementation. No cloud timer was disabled.
The already-started read-only replica transfer is separate from new scheduled
work and has not been promoted as a deployment backup.

On October 5, 2026, the first local worker successfully pulled fresh, bounded
read-only evidence from AWS and completed the paper-performance audit, forecast
skill audit and fixed bias-only comparison. These are retrospective diagnostics,
not V7 profit evidence or automatic model promotion. Runs and exports are
preserved under ignored `.local/v7-offload/research/runs/`; `latest-success.json`
points to the last completed run. A failed attempt preserves that receipt and
records its own failure. Receipts bind output hashes, export hash, Python version,
Git revision and Python source-file hashes. Separate accounts stay separate.

## Scheduling and access

`scripts/local_compute/worker.py` takes a private JSON configuration containing
`ssh_target`, `ssh_key` and `state_dir`. Keep this file in ignored `.local`, mode
0600. The remote exporter runs SQLite in read-only/query-only transactions, caps
each table at 250,000 rows, aborts on truncation, and limits query and SSH runtime.
It performs no AWS writes. The local process uses an exclusive lock to avoid
concurrent runs. Failed SSH or analysis never overwrites successful results.

Install the six-hour schedule with:

```bash
python3 scripts/local_compute/install_launch_agent.py \
  --config .local/v7-offload/worker-config.json
```

When explicitly enabled, the user LaunchAgent `com.weatheredge.v7.local-research` runs at login and every
six hours. Its `caffeinate -i` assertion lasts only while a job runs. It uses low
CPU/IO priority and does not keep a closed-lid Mac or a disconnected network
available. It runs with the signed-in user's existing SSH access; no AWS keys
are copied to the Mac. Inspect `.local/v7-offload/research/status.json` for current
status and private launchd logs for scheduling errors. Disable this exact job:

```bash
launchctl disable "gui/$(id -u)/com.weatheredge.v7.local-research"
launchctl bootout "gui/$(id -u)/com.weatheredge.v7.local-research"
```

## Full database replicas

`scripts/local_compute/replicate_database.py` uses the official `sqlite3_rsync`
tool, which understands SQLite transactions and can copy a live origin safely.
Ordinary file rsync cannot safely copy a busy SQLite database. Install matched
SQLite tools on both ends, verify vendor download hashes, and supply the remote
executable and a private SSH wrapper. See [SQLite's documentation](https://sqlite.org/rsync.html).
The first transfer sends roughly the database size; subsequent incremental
copies can be smaller, but this script deliberately requires a new destination
to preserve verified historical snapshots. Bandwidth and local disk are real
costs. A long read transaction can pin origin WAL pages; monitor AWS disk/WAL
and cancel rather than exhaust the server.

A successful receipt requires SQLite integrity and foreign-key checks plus a
local SHA-256 digest. An interrupted file without a receipt is unverified.
The snapshot clock is copy start, not completion. Header bytes may differ from
the live origin; physical hash equality is not a valid origin-parity test.
This local receipt **does not satisfy the existing durable S3 backup/restore
and cutover gate**. Do not deploy V7 or prune AWS journal history merely because
a replica exists. Durable off-host backup integration and full analysis promotion
need their own tested provenance/restore contract.

## Cost boundaries

Local work does not change EC2's fixed hourly price. Downsizing requires moving
the actual high-memory production jobs and measuring their replacement before
reducing memory; a quiet instantaneous memory reading is insufficient. The
current implementation offloads research diagnostics, not the still-pending full
production Strategy Lab builder or deployment gate. No AWS timer was disabled.

The authenticated October 5, 22:31 PDT bill showed **$8.00 accrued**, not a full
monthly bill: EC2 $5.58, Lightsail $1.17, S3 $0.66 and VPC $0.59. The Lightsail
line was unused static IPs, and the inventory showed two unattached allocations
and no instances. At $0.005 per allocation-hour, two allocations cost roughly
$7.30 per 730-hour month. Releasing them requires action-time confirmation because
the addresses cannot be recovered. Past charges remain payable. The older
$51.19 recurring estimate omitted this leftover resource and is not a verified
current monthly forecast.

## Checks

```bash
python3 -m unittest discover -s trading/tests -p 'test_local_compute.py' -v
python3 -m compileall -q scripts/local_compute
```

Tests verify the exporter leaves source database bytes unchanged, rejects a
truncated export and preserves last-success evidence after a disconnected run.

The user removed the first unused Lightsail allocation during this task. A
fresh console inspection still showed one unattached allocation. Its approximate
remaining cost is $3.65 per 730-hour month; no assistant deletion ran.
