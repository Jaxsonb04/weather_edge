# GitHub, AWS, and Local Consistency Audit

Date: 2026-07-24 (America/Los_Angeles)

Status: **DONE WITH CONCERNS**

This audit compared the GitHub repository and pull-request state, every local
worktree and runtime cache, the active EC2 runtime, the retired Lightsail host
reachable from the recorded operator environment, and the documented AWS
deployment model. It also ran the complete Python and web test suites, dependency
audit, bundle checks, compilation, shell syntax checks, and production service
inspection.

## Executive findings

1. **Production publication lock starvation was real and active.** Strategy Lab
   held the shared publication lock for more than nine minutes while the
   five-minute operational publisher waited. The freshness watchdog then
   reported an 18.2-minute-old manifest. PR #53 separates research generation
   from the publication critical section, makes the operational cycle the sole
   publisher, and bypasses the ten-minute CDN cache during watchdog checks. It
   was green, behavior-neutral for trading, merged as `46b0b718`, and deployed
   through the guarded database-backup path during this audit.

2. **Four archive-backed SQLite streams did not share a bounded retention
   policy.** `decision_snapshots` was pruned, but `probability_snapshots` and
   `paper_monitor_snapshots` grew forever, as could orphaned forecast/market
   parents. The audit-hardening change keeps 45 online days and removes only
   older rows after the existing lossless archive gate. Approved decisions and
   referenced research context remain untouched.

3. **The web dependency graph contained three known vulnerabilities.** The lock
   contained vulnerable PostCSS, node-tar, and DOMPurify versions, including a
   high-severity PostCSS path-traversal advisory published on the audit date.
   Exact patched overrides now make `bun audit` report zero vulnerabilities.

4. **Cloud-account cleanup is incomplete.** The retired Lightsail host remains
   reachable and has no WeatherEdge timers. Old EC2 operator environments are
   also present locally. Two AWS SSO authorization attempts expired, so the
   account API could not prove instance, volume, elastic-IP, snapshot, lifecycle,
   or IAM state. No cloud resource was terminated from a stale local identifier.

## Reconciliation snapshot

### GitHub

- Protected default branch: `main`; strict Python 3.12, Python 3.13, and Web
  checks; force-push and deletion disabled.
- PR #53 was mergeable with all required checks passing and was merged.
- PR #52 remains a draft because it contains paper-signal behavior changes that
  explicitly require separate approval.
- PR #29 remains open and conflicted; it contains a distinct Strategy Lab query
  optimization and was not treated as disposable.
- Automatic deletion of merged branches is now enabled.
- Two merged local branches and one merged remote release branch were removed.
- GitHub Pages remains intentionally unprotected because the EC2 publisher writes
  the deployment branch directly.
- Secret scanning and push protection are enabled. Code scanning has no recorded
  analysis through the repository API; CI Semgrep is the current static-analysis
  control.

### Active EC2

- Runtime source initially matched GitHub `main` at `ac21ec48`; it was not a
  hybrid local tree. The public manifest carried the same provenance.
- The host is a `t4g.medium`, Ubuntu 24.04 ARM instance with a 38 GB root volume.
- The paper database was approximately 8.6 GB; the forecast database was
  approximately 465 MB.
- Snapshot counts at inspection included approximately 653k decisions, 560k
  probabilities, 379k monitor rows, 94k market rows, and 93k forecast rows.
- Transient `database is locked` monitor/settlement failures and GitHub SSH
  timeouts were visible in the journal. Timers retried, but failed unit state
  remained latched until a later successful run.
- Strategy Lab repeatedly reached its old 15-minute timeout while staying inside
  its memory cap, leaving the strategy artifact stale. Now that research no
  longer holds the publication lock, its contained timeout is 30 minutes. The
  timer now waits 15 minutes after completion instead of measuring from start;
  a 21-minute run therefore cannot immediately launch another memory-heavy run.
- Both operational and Strategy Lab timers fetched and reset the same Git source
  cache before each build. This creates avoidable GitHub availability coupling
  and mutable-source work on the runtime host.
- The instance role is intentionally limited to S3 backup/archive access; it
  cannot inventory the AWS account.

### Local workspace

- The root checkout was clean before the audit. Other worktrees with divergent,
  unpushed, or untracked work were preserved.
- The repository occupied approximately 5.0 GB, including a stale 3.3 GB
  migration staging copy. Cleanup reduced it to approximately 1.6 GB.
- Runtime JSON/cache state was replaced with the project’s explicit AWS-authority
  placeholders using `scripts/clear_local_runtime_state.py --confirm`.

## Cleanup performed

### Local

- Removed the stale `.local/migration-stage` copy, monthly preview, empty audit
  directory, generated `weatheredge.egg-info`, `.DS_Store`, Python bytecode, and
  pytest caches.
- Preserved `node_modules`, the development virtual environment, keys/operator
  environments, and every worktree containing unique work.
- Added `*.egg-info` to the full deployment rsync exclusions so generated package
  metadata is recreated remotely instead of copied as source.

### Active EC2

- Verified the retained local database snapshot against both its local checksum
  and S3 checksum/object size before removing one older 4.1 GB local copy.
- Root-volume usage dropped from 68% to 58% before the guarded deployment backup.
- Changed local database-backup retention from seven days to one. The verified S3
  lifecycle remains the durable 35-day tier.
- Added a pre-quiesce disk-capacity gate for the snapshot plus downloaded restore
  copy and 1 GiB headroom. The first guarded deploy attempt exposed this missing
  check by reaching 89% disk and failing its restore download before source
  transfer; the exact prior timer set was restored before retry.
- Removed the unused Lightsail-era forecaster-refresh environment flag.
- Removed three retired remote test/helper files that were absent from `main`.
- Preserved the archive ring buffer, active databases, current rollback build,
  runtime models/caches, and S3 backup history.

### GitHub

- Enabled `delete_branch_on_merge`.
- Deleted only branches proven merged: two local deep-session branches and the
  remote `release/deep-session-20260719` branch.
- Merged and deleted the source branch for PR #53 after rechecking its exact head,
  merge state, and three required checks.

## Remaining concerns and recommendations

### Requires AWS account authorization

Use the `weatheredge-admin` SSO profile, then inventory all regions before
terminating anything. Confirm the active EC2 instance by source/runtime
provenance, verify snapshots or S3 recovery for each retired host, then remove:

- the retired Lightsail instance and any static IP/disk/snapshot no longer
  required;
- the quiesced old east-region EC2 instance and orphaned volume/IP/security-group
  resources;
- any abandoned west-region migration instance or unattached volumes.

### Requires an explicit architecture choice

The architecture review identified six bounded seams:

1. formal forecast archive interface and schema;
2. immutable release module instead of per-timer Git resets;
3. centralized runtime path resolution;
4. one parameterized systemd installer;
5. coherent publication transaction/catalog;
6. deliberate retirement plan for compatibility facades.

The immutable release module is the highest operational leverage after the
forecast archive seam: it removes GitHub SSH from every timer tick, prevents
source mutation during running services, and makes rollback/provenance simpler.

### Known workload that remains

- Source-neutral scan contexts are intentionally preserved for chronological
  research even when no decision references them. They remain an unbounded
  online dataset until the formal archive interface is chosen.
- The guarded deployment performs an integrity scan before upload and again after
  download. This is safe but slow on an 8+ GB journal; bounded retention and an
  immutable release path should reduce the operational cost before changing the
  recovery gate. The new capacity check prevents this slow phase from beginning
  when both verification copies cannot fit.
- Local `verify_project.sh` requires Semgrep, while the preliminary health checker
  labels Semgrep optional. CI installs and runs the pinned Semgrep version, but
  local messaging should be made consistent.

## Verification evidence

- Python suite: 2,373 tests passed on the rebased hardening commit.
- Targeted hardening/deployment/archive suite: 151 tests passed.
- Frontend: 138 tests passed; lint and production build passed.
- Bundle budgets passed.
- Python compilation and shell syntax checks passed.
- `bun audit`: zero vulnerabilities after patched overrides.
- Local health score for runnable categories: 10/10. Semgrep, shellcheck, knip,
  and gbrain categories were not scored when their local tools/configuration were
  unavailable.
