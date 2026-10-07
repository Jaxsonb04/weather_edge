# V7 hybrid research workflow

AWS owns continuous weather/provider collection, final station truth, the paper
ledger, monitoring, settlement, readiness and dashboard publication. The home Mac
owns bounded offline audits, small machine-learning comparisons and a separate
prospective weather shadow. A sleeping or disconnected Mac does not stop AWS or
alter an order, account, strategy gate or trading policy. Real-money execution
stays disabled until the owner separately authorizes it.

The October 5 new-session request authorizes designing and implementing this
bounded workflow, superseding the earlier request to disable scheduled research
pending review. It does not authorize cloud purchases, increased spend, intensive
continuous Mac use, deletion of research history or live-money trading.

## Work assignment

| Work | Owner | Failure behavior |
| --- | --- | --- |
| Authoritative paper collection, scan, monitor, finality-gated settlement | Existing AWS runtime | Existing scheduler and safety gates remain binding |
| Public JSON, provenance manifest and app publication | Existing AWS publisher | Mac output is never automatically published |
| Account audit, retrospective weather scoring, fixed bias comparison | Mac | Preserve last successful private receipt; distinguish retained input from new evidence |
| CRPS location/scale and shallow quantile boosting comparison | Mac | Chronological holdout, original availability checks, no automatic model promotion |
| Prospective free-provider shadow evidence | Mac, in a separate private database | Skip on resource/power/API guard; never substitute its identity for AWS paper execution |
| Expensive full historical Strategy builder | Mac manual helper, once complete verified inputs exist | Produce private staged cache; no cloud transfer or promotion |
| Backend V7 cutover and any later smaller instance | Guarded operator deployment | Existing backup, lineage, source, accounting and publication gates must pass |

The source supports twenty cities; the last verified old backend supported
fifteen. The deployed app may therefore truthfully display V7 source improvements
while the active runtime/account remains V6. Old paper losses and economically
separate research accounts must retain their original identity. Neither a local
ML score improvement nor a static app release starts V7 trading performance.

## Mac safeguards and schedule

The reviewed installer creates a two-hour schedule as the signed-in user's
background LaunchAgent. Installation does not start a run immediately. Its job-scoped `caffeinate -i`
assertion ends with the job; it does not change system charging, fan, frequency,
sleep or battery settings and does not promise closed-lid availability.

Admission requires AC power, low-power mode off, no native thermal/performance
warning, at least 25% reported memory headroom, one-minute load no greater than
25% of the logical CPU count, and at least 20 GiB free local storage. Admission
fails closed when a required native observation is unavailable. The same host
checks repeat during computation. The worker has an exclusive lock shared with
the manual full-analysis helper.

Each scheduled run must have a wall-clock budget of at most five minutes and
an observed CPU-time budget of at most two minutes. The installer rejects a
weaker budget before creating a state directory, writing its plist or invoking
`launchctl`. Installed arguments include `--scheduled`; the worker revalidates
the same ceilings before state changes or work on every invocation, so later
private configuration edits cannot weaken the background budget. The sampled
aggregate process-group RSS budget remains 2 GiB. Numerical libraries
use one thread. A process-group supervisor paces aggregate work to half of one
logical CPU with short bursts and samples descendants every quarter second;
resource or time failures terminate the entire group, including paused children.
These are conservative implementation policy limits, not laboratory proof of
battery lifetime or hardware temperature. Native monitoring and sampled RSS
cannot guarantee zero transient overshoot. On the inspected 16-logical-CPU,
48-GiB Mac, the pacing target is about 3.1% of aggregate CPU capacity while a
job is active. No GPU/deep-learning workload or overclocking is installed.

Twelve regular runs per day permit at most 60 minutes of wall time and 24 minutes
of observed CPU time, below the original six-hour schedule's theoretical 80-minute
wall and 40-minute CPU totals. The two observed initial full runs used about
20 seconds wall time and 8.6 CPU seconds each; extrapolating that sample gives
about four wall minutes and 103 CPU seconds per day, not a guaranteed future
duration. The manual full Strategy helper retains its separate 20-minute wall
and ten-minute CPU budget. Both entry points share the lock and native checks.

Apple identifies temperature history and charging pattern as battery-aging
factors, recommends Optimized Battery Charging, and recommends a ventilated
stable surface and 10–35°C ambient operating temperature. The worker preserves
existing settings rather than changing charging policy. [Apple battery guidance](https://support.apple.com/en-us/102338),
[Apple temperature guidance](https://support.apple.com/en-us/102336).

The private configuration stays under ignored `.local` with mode 0600. Enable
only the documented flags appropriate to the tested local installation:

```json
{
  "state_dir": ".local/v7-offload/research",
  "offline_export_dir": "<private retained export directory>",
  "fresh_export": false,
  "ml_boosting": true,
  "local_prospective_collection": true,
  "shadow_cities": ["mia", "lax", "chi", "atl"],
  "shadow_rotate_registry": true,
  "limits": {
    "wall_seconds": 300,
    "cpu_seconds": 120
  }
}
```

Existing SSH access values, if present, remain private. The fixed shadow hook
allows at most four city slugs and eight HTTP requests per run. It uses only the
collector's approved free-provider path and its separate shadow database; it
never invokes an AWS API, paid weather credential, ledger writer or publisher.
With `shadow_rotate_registry: true`, the city-list length selects a bounded batch
from the twenty-city registry. Four-city batches on the two-hour schedule revisit
a city approximately every ten hours when admission passes. The durable ceiling
remains eight requests per run and 96 per UTC day, including failed and manual
calls. Twelve regular eight-request batches exhaust that allowance; further calls
are refused without retries, paid fallback or raising the budget. Provider access
remains educational, noncommercial research. [Open-Meteo terms](https://open-meteo.com/en/terms).

The shorter revisit reduces missed full-day CLI outcomes. NWS describes an early
morning prior-day report and a late-afternoon current-day report; its Chicago
office publishes full summaries after 00:30 CST/01:30 CDT and intermediate
summaries after 16:30, a roughly 15–16-hour window. A ten-hour revisit better fits
that window than the original thirty-hour revisit. Issuance differs by office,
intermediate or corrected products may intervene, and outages, sleeping, loss of
AC power or resource deferrals still create gaps. Latest-only retrieval does not
recover a missed report. Missing final truth stays explicit; the project's
complete-day CLI finality is distinct from later NCEI certification. [NWS CLI
format](https://www.weather.gov/media/notification/pdfs/scn00-56.pdf), [NWS observation
FAQ](https://www.weather.gov/lot/weather_observations_faq).
When the shadow hook is disabled and input hashes plus Python source are
unchanged, duplicate analysis is skipped. A source-only reanalysis is explicitly
different from new evidence. Source/Git identity is checked again before a
successful receipt is installed, rejecting a run whose code changed underneath it.

Use the existing development Python so boosting can use already-installed
scikit-learn; no new dependency installation is necessary:

```bash
.venv-dev/bin/python scripts/local_compute/worker.py \
  --config .local/v7-offload/worker-config.json --scheduled --checks-only
.venv-dev/bin/python scripts/local_compute/install_launch_agent.py \
  --config .local/v7-offload/worker-config.json
```

Status and receipts live under the configured private state directory. Every
successful result binds Python version, source commit/file hashes, input hashes,
output/lineage hashes and observed resource use. Failures preserve the previous
success. Disable this exact local job with:

```bash
launchctl disable "gui/$(id -u)/com.weatheredge.v7.local-research"
launchctl bootout "gui/$(id -u)/com.weatheredge.v7.local-research"
```

The original six-hour schedule with 20-minute wall and ten-minute CPU budgets
was installed and independently inspected on October 5, 2026. It
was enabled and loaded, with no run at installation. The first complete guarded
worker finished at 23:32 PDT: all four audit/bias/ML jobs succeeded in 20.335
wall seconds, with 8.57 observed CPU seconds and 366.9 MiB peak group RSS.
Eight free-provider requests collected twelve new original lead-0/1/2 vintages
for Miami, Los Angeles, Chicago and Atlanta. Together with the retained initial
SFO sample, fifteen vintages were present; zero had a completed scored target.
The four-city rotation cursor advanced to four of twenty. This was a dirty-source
diagnostic run with complete source/input hashes, not a backend release.

The ML result reproduced 6,656 reconstructed cases over 246 calendar targets and
the same negative challenger comparisons. Original-vintage strict prequential
cases remained zero; live allocation remained $0 and no bankroll recommendation
was produced. No AWS export, full replica, ledger mutation or promotion ran.

A second integrated run finished at October 5, 23:50 PDT from clean source
`7e39093de`. Its next four-city batch added twelve vintages (twenty-seven retained)
using eight requests; all four research jobs completed in 20.013 wall seconds,
8.6 observed CPU seconds and 366.8 MiB maximum sampled group RSS. No scored
issued target was available. This run's dated, allowlisted collection counts and
resource measurements are included in `public/hybrid_research.json`, separately
from the unchanged zero-qualified-vintage reconstructed comparison.

Public research snapshots are deliberately reviewed releases, not automatic Mac
uploads. `scripts/build_hybrid_research_summary.py` can take the optional paired
`--worker-receipt` and `--collector-receipt` files, alongside `--report` and
`--output`. It verifies their exact hash relationship, clocks, research-only
identity, limits and observed usage before copying only dated counts, resource
measurements and receipt hashes. Private configuration, source maps, paths and
account identifiers are excluded. A failed pair preserves the prior artifact.
Future private collections do not automatically change the webpage's dated
snapshot or its AWS runtime identity.

The October 6 scheduled-run audit found that the five cities absent from the
retained AWS seed have no matched NWP training dates. The sixty-date training
minimum still applies. Their valid free-provider responses are preserved as raw
HTTP evidence, but this collector does not yet convert them into training
archive rows. They cannot become trained simply by waiting for more scheduled
runs. A future onboarding path must preserve source, station, forecast lead,
retrieval clock and final truth provenance without inventing historical
availability or lowering the training minimum. Raw-only collection is distinct
from issued distributions, model promotion and trading evidence.

The worker accepts the existing collector's raw-only exit only when the current
attempt has a fresh exact research receipt, an unchanged source identity, a
matching weather-export hash and the collector's exact final completion summary.
A receipt alone is insufficient: collection can fail while writing the rotation
after that receipt has been saved. The worker records both the raw-only status
and zero new distributions while continuing the four offline audits. Other
child failures and stale, malformed or mismatched completion evidence preserve
the prior successful analysis and remain failures.

On macOS, group signaling after a child exits but before it is reaped can report
permission denied. The supervisor reaps its known child before recovering only
that exit race, then retries group cleanup to terminate surviving descendants.
Permission failures for a live child remain errors. Any unproved cleanup is a
distinct fatal failure, even if collection otherwise finished or a resource
guard deferred work; it cannot borrow raw-only success or benign deferral.
Private failure logs preserve traceback and cause with owner-only permissions.

The installed clean-source repair verification finished October 6, 18:22 PDT
on `707ad9cf2`. It used 108.714 wall seconds, 52.24 observed CPU seconds and
370.5 MiB sampled group RSS; eight requests added twelve distributions. All
four audits and five output/lineage hashes passed. The private store reached
105 versions across fifteen stations; this is not 105 independent resolved
outcomes. The two-hour LaunchAgent is enabled, limits remain 300/120 seconds,
and recurring AWS exports remain off. The webpage retains its explicitly dated
earlier reviewed batch. These newer timings also show why the initial 20-second
runs must not be treated as a guaranteed recurring duration.

The October 6 refinement requires the two-hour schedule and tighter 300/120-second
budgets above. The dated October 5 runs retain their original limits and identity;
they are not relabeled as runs of the refined schedule. At October 6, 00:14 PDT,
the private configuration was tightened, the two-hour job independently installed
and native admission checked. The clean-source `--scheduled` execution completed
00:15:53 PDT at `18deb2d6b`: eight requests, twelve new vintages (39 retained),
all four jobs successful, 19.909 wall seconds, 8.14 observed CPU seconds and
367.8 MiB sampled peak group RSS. Source/input hashes matched, five/two-minute
limits passed, and AWS export and live execution remained disabled. PR #130
merged at 00:20:58 PDT; the guarded static app release and public desktop/mobile
canary passed. The webpage intentionally keeps its reviewed October 5, 23:50
batch; subsequent private runs require another reviewed snapshot to be published.

## Network and billing boundary

Scheduled AWS export is disabled by default. AWS states that internet egress
has a 100-GB monthly free allowance aggregated across services and regions
(except China/GovCloud); that statement does not establish how much this account
has already used. The existing full replica consumed roughly 31 GiB of transfer,
and billing observations may lag. Fresh remote pulls require a private
current-month allowance verified after other usage and a safety margin. [AWS EC2
pricing](https://aws.amazon.com/ec2/pricing/on-demand/).

The worker reserves the entire worst-case export ceiling before opening SSH,
keeps the reservation even after failure, caps payload at half the ceiling, and
rejects expired/exhausted allowances. Reviewed ceilings are 256 MiB per attempt
and 512 MiB reserved per month, never automatic full database replication. A
missing allowance defers remote work. Direct free-provider shadow collection
uses the home's connection and does not consume AWS egress. No new AWS service,
volume, endpoint, paid commitment or subscription is provisioned.

Moving calculations off an always-running instance alone does not reduce its
fixed running-time charge. Reducing the bill requires eliminating an unused paid
resource or measuring and safely reducing the actual provisioned footprint.
AWS documents that On-Demand instance pricing is fixed per running second.
[AWS On-Demand lifecycle pricing](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-on-demand-instances.html).
No downsize is claimed from a quiet instantaneous memory observation. The
continuous workload and at least one representative full analysis require
measurement before any smaller instance is selected.

At October 5, 23:04 PDT, authenticated billing showed $8.00 month-to-date,
$54.50 monthly forecast and $52.81 September actual; these are dated account
observations, not a guarantee. Two unused Lightsail allocations were previously
released by the user, with the later empty inventory confirmed. Past charges
remain payable. A local-worker receipt is not evidence that the forecast has
already fallen.

## Full database replicas and actual Strategy offload

`scripts/local_compute/replicate_database.py` uses SQLite's transaction-aware
`sqlite3_rsync`; ordinary file copying cannot establish a consistent live SQLite
snapshot. A long copy can pin WAL and consume server space. No new full copy is
scheduled. The completed October 5 paper replica receipt records SQLite integrity,
foreign-key verification and a local SHA-256 over 33,322,299,392 bytes. A failed
copy without a receipt remains unverified. The copy-start clock is the snapshot
clock. Physical origin/replica header hashes can legitimately differ.
[SQLite remote-copy documentation](https://sqlite.org/rsync.html).

`scripts/local_compute/full_analysis.py` invokes the actual full Strategy
builder, rather than a smaller audit labeled as offload. It requires verified
paper **and complete weather** snapshot receipts, matching file hashes, exact
source identity, an allowlisted non-secret runtime configuration, the expected
strategy fingerprint and staged forecaster provenance/JSON inputs. It creates
separate APFS copy-on-write working clones with `cp -c`, aborts if cloning is
unavailable and never falls back to a large unbounded copy. Original snapshots
are retained. Zero-length WAL files are harmless; nonempty transactional
sidecars are rejected.

The manual helper retains its separate 20-minute wall/ten-minute observed CPU
budget, the same native safeguards and shared lock, and installs a Python socket/DNS
audit guard against network access. The inspected builder has no network subprocess;
the hook is not a general operating-system network sandbox. It
uses the direct diagnostic builder, and checks source identity again before
writing its cache/private-evidence receipt. A successful receipt remains
`promotion_eligible: false`; clean source and validated cache are separate facts.
The AWS importer must independently verify deployed source/configuration,
complete snapshot lineage, current cutover state and its own publication gate.
A full weather snapshot is presently a required input, not something silently
reconstructed from the three-table research export. The manual helper does not
remove the stale-analysis warning until actual gated import and backend cutover
succeed.

The local manifest schema contains `state_dir`, `target_source_sha`,
`expected_config_fingerprint`, `runtime_config` (only `PAPER_BANKROLL`,
`PAPER_ENTRY_MODE`, `PAPER_RISK_PROFILE`), `calibration_min_train`,
`paper_snapshot` and `weather_snapshot` (each `path`, `receipt`, `sha256`), and
`forecaster_files` (allowlisted names mapped to `path`/`sha256`). Paths and access
values stay private. Diagnostic dirty-source runs require explicit
`allow_dirty_diagnostics: true` and cannot be promoted.

FileVault was confirmed on for the Mac, but an encrypted local disk plus one
replica receipt does **not** satisfy the existing durable S3 restore/cutover
contract. No cloud journal history is deleted, no archive-only policy changes,
and no backup gate is bypassed. Complete verified weather inputs, durable
recovery integration and source-matched cache import remain separate work.

## Validation and decision boundary

Focused tests exercise read-only export preservation, actual prospective member
schema/unknown fields, non-finite limits, power/thermal/memory/load rejection,
process-group termination, free-egress reservation, duplicate skipping,
source/input-change rejection, shared locks, snapshot tampering, and actual
SIGTERM/SIGHUP cancellation of a supervisor with a separate child session.
Schedule regressions verify two-hour/background/no-immediate-run settings and
prove weak, missing, nonfinite or relaxed scheduled limits leave the existing
plist, state directory and `launchctl` untouched. Runtime regressions prove
that a subsequently weakened private configuration starts no admission,
subprocess, network, analysis or state mutation in installed scheduled mode.
The final affected gate passed 165 tests and 46 subtests. Exact-candidate CI
passed both Python versions with 3,732 tests, eleven skips and 46 subtests,
218 web tests, build, browser-observed bundle budgets and zero blocking Semgrep
findings. Public desktop/iPhone-emulated/320-pixel checks passed. Local compilation
and the focused suite must pass before installation; the worker must complete a
bounded real run before its results are called verified.

```bash
.venv-dev/bin/python -m unittest discover -s trading/tests \
  -p 'test_local_compute.py' -v
.venv-dev/bin/python -m compileall -q scripts/local_compute
```

October 31 is a review deadline, not automatic permission to activate real
money. New shadow forecasts are weather evidence; they are not original AWS
execution vintages or proof of after-fee profitability. Bankroll selection stays
conditional on independent edge, drawdown and liquidity evidence; live allocation
is $0 while authorization and readiness remain absent. Existing $1,000 paper
accounts are separate simulation ledgers, not a recommended initial investment.
