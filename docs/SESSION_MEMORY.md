# WeatherEdge Session Memory

Last updated: 2026-07-26 00:12 PDT

Last production verification: 2026-07-25 23:52 PDT

Status: production healthy and paper-only at the last verification

This is the rolling cross-session handoff for WeatherEdge. It records the last
verified state and the reasoning behind it. It is not a substitute for checking
current AWS state before making an operational claim.

## Session Brief

- **Production baseline:** local `main`, GitHub `main`, the EC2 build, and public
  artifact provenance matched clean revision
  `cd58c8c749c527519642fc62b9a39fce38d04abf`.
- **Last failure:** Strategy Lab's bounded tail query used parameterized SQLite
  limits. Production SQLite chose a full scan over roughly 694,000 decision
  snapshots, taking about 92 seconds for one query and pushing the service into
  its 120-second timeout. Public Strategy data then fell behind.
- **Recovery:** PRs #55 and #56 bounded recurring work, restored the observed
  indexed query plan with sanitized integer literals, added a deterministic
  query-work regression test, and separated unsafe nightly maintenance windows.
- **Performance snapshot:** repeated production systemd Strategy refreshes
  completed in roughly 10.6-11.7 seconds. The post-rebuild public artifact had zero
  forecast health warnings.
- **Runtime health:** all 23 canonical systemd units matched source, all 11
  timers were enabled and active, the failed-unit count was zero, and the
  weather database passed its SQLite quick check and a separate foreign-key
  check with zero errors.
- **Safety:** real-money trading was disabled and dry-run enforcement was on.
  Enabled order-placement controls were for isolated paper accounts only.
- **Performance evidence:** the live paper account realized `+$9.9765` on
  2026-07-20. The `+$17.4301` Strategy Lab total that day combined economically
  separate paper accounts and must never be described as one bankroll's return.
  The active `$16/day` target-v2 research account had zero trades and zero P&L
  because no candidate passed its after-fee and risk gates.
- **Cleanup:** the verified EC2 backup duplicate was removed after its encrypted
  S3 object and checksum matched, reducing root-disk use from 69% to about
  45-46%. Merged local worktrees, stale runtime files, and disposable caches
  were removed; unique unpushed and open-PR work was preserved.
- **Deliberate follow-up:** retain the narrowly scoped single-IP SSH rule for the
  owner's next AWS session. Revoke it only when the owner says that access is no
  longer needed. Exact access identifiers stay in gitignored operator state.

## What Went Wrong

### Strategy query-plan regression

The recurring Strategy build was intended to inspect only a bounded tail of
recent scan contexts. Its internal row and context caps were passed to SQLite as
bound `LIMIT` parameters. On production SQLite 3.45, that form selected a full
table scan instead of `idx_decision_snapshots_scan_context`.

The newest 64 contexts contained only 1,176 rows, below the 2,048-row cap. That
real data shape exposed the planner behavior that small development fixtures did
not. The scan performed roughly 1.01 GB of physical reads and 1.79 GB of logical
reads before the overall service timed out.

### Nightly database contention

The paper-database prune and dataset-backfill schedules overlapped. One observed
prune ran from about 02:22Z to 02:34Z while the backfill began around 02:25Z.
The backfill exhausted three SQLite busy retries.

Both heavy timers were also persistent. After a deployment or reboot, missed
windows could therefore launch both jobs as catch-up work and recreate the same
contention.

### Stale publication during recovery

The first public Strategy artifact after deployment was generated before the
controlled dataset rebuild completed, so it still reported stale NWP data. A
fresh Strategy build after the rebuild saw eight-model rolling-target coverage,
fresh timestamps, and no warnings, then the operational publisher promoted the
correct artifact.

### Misleading local runtime state

Ignored local databases and generated JSON files were stale but looked
production-like. They are not valid production evidence. AWS-generated runtime
data and the published manifest are authoritative after synchronization.

### Location-dependent access

The operator's public IP changed between houses, so the narrow SSH allowlist no
longer matched. Access was restored with a temporary single-IP rule. The owner
asked to retain it for the next session; it is deliberately not marked as an
unfinished cleanup error.

## What We Accomplished

### Source and GitHub

- Merged PR #55 at `cb804e8537abc11db820d269e04139bbc0b51ebd`.
- Merged PR #56 and deployed merge revision
  `cd58c8c749c527519642fc62b9a39fce38d04abf`.
- Verified local `main`, `origin/main`, GitHub `main`, EC2 build provenance, and
  public artifact provenance agreed.
- Passed 2,414 local tests, Python 3.12 CI, Python 3.13 CI, web CI, the Bun
  production build, Python compilation, shell syntax checks, Semgrep with zero
  findings, and three independent final reviews.

### Strategy performance and correctness

- Replaced the two internal query limits with positive integer-sanitized SQL
  literals while preserving the query's predicates, ordering, and caps.
- Added a deterministic SQLite virtual-machine work-limit test that fails the
  old full-scan implementation and guards bounded query work without depending
  on wall-clock timing.
- Reduced the recurring Strategy service from the timeout path to about 11
  seconds across repeated production runs.
- Kept the expensive historical analysis in its contained cache workflow while
  keeping recurring public refresh bounded.

### Timer and maintenance reliability

- Scheduled prune for 09:20 UTC with up to 300 seconds of randomized delay.
- Scheduled dataset backfill for 10:01 UTC with up to 120 seconds of randomized
  delay.
- Set `AccuracySec=1s` for both heavy timers.
- Set both heavy timers to `Persistent=false`, preventing missed-window
  catch-up from launching them concurrently after deploys or reboots.
- Preserved persistent behavior for frequent operational timers.

### Data recovery and publication

- Ran a controlled rebuild with all producer timers safely quiesced and restored
  by an exit trap.
- Refreshed IEM truth, NWP archive data, and EMOS lead 1 and lead 2 across all
  fifteen cities.
- Completed the dataset service successfully with a measured 119.2 MB memory
  peak and no swap.
- Rebuilt Strategy after the data refresh, published it, waited for exact public
  manifest parity, and reran the freshness watchdog successfully.
- Verified the local and public manifest and Strategy files were byte-identical.

### Backup and cleanup

- Created and fully verified a deployment snapshot before source changes.
- Verified the retained S3 object was 9,367,420,928 bytes, AES256-encrypted, and
  matched SHA-256
  `dc9dd1ec708b6d8d63c499193ee14aebcfb10a131a1ad66a948da0a954e2468b`.
- Removed only the redundant EC2-local copy after that verification, increasing
  free disk from roughly 12 GB to 21 GB.
- Removed three clean, already integrated worktrees and obsolete local branches.
- Cleared stale local runtime state through the canonical repository cleanup
  workflow.
- Removed assistant, test, build, bytecode, and inactive dependency caches.
- Preserved unique unpushed reliability work, open strategy work, learned model
  artifacts, source weather data, and active development dependencies.

## Paper Performance Snapshot

The following figures were generated from logical resolved paper lots. They are
research evidence, not promised returns.

| Date | Live paper account | Cross-account Strategy Lab total |
| --- | ---: | ---: |
| 2026-07-20 | +$9.9765 | +$17.4301 |
| 2026-07-21 | +$9.91 | +$18.89 |
| 2026-07-22 | +$7.57 | +$18.22 |
| 2026-07-23 | +$0.83 | +$11.58 |
| 2026-07-24 | +$0.20 | +$7.95 |
| 2026-07-25 | +$0.00 | +$4.39 |

The cross-account column combines live, shadow, motion, and target research
accounts. It cannot be summed or described as if one account earned it.

At the snapshot:

- Live paper equity: `$958.38`.
- Live all-time realized P&L: `-$41.62`.
- Live open positions: zero.
- Active target-v2: zero trades, zero realized P&L, zero open positions.
- The `$16/day` target was explicitly non-guaranteed and infeasible under the
  available conservative expected-profit evidence.
- All 24 inspected opportunities remained `NO_TRADE` under after-fee
  lower-confidence-bound and risk gates.

## Safety And Interpretation Rules

- Never enable real-money trading as part of an audit, performance recovery, or
  target-seeking change.
- Never promise `$16/day` or any return. A target is a paper research KPI.
- Never weaken a `NO_TRADE`, after-fee edge, loss-pause, exposure, calibration,
  or evidence gate merely to increase activity.
- Report one account separately from cross-account research attribution.
- Treat pre-current-evidence legacy outcomes as unverified when the artifact
  labels them that way.
- Keep AWS access identifiers and credentials out of this file and all tracked
  project artifacts.

## Known Nonblocking Concern

The LAMP and GFS-MOS NOAA archive paths returned HTTP 403 for all attempted
cycles during the rebuild. The backfill skipped those sources safely; IEM,
Open-Meteo previous runs, NBM, HRRR, truth, and EMOS work completed. If the 403
responses persist, remove or replace those fetch paths so the nightly job does
not spend time on requests that cannot succeed.

## Next Session Checklist

1. Print the `Session Brief` above.
2. Confirm the checkout is clean and compare local `HEAD` with `origin/main`.
3. When current production state matters, revalidate build provenance, failed
   units, timer state, public-manifest parity, freshness, disk, and real-money
   safety flags from AWS.
4. Keep the single-IP SSH rule until the owner says access is finished.
5. Observe the next natural prune and dataset-backfill runs for schedule
   separation and successful completion.
6. Update this file after any deployment, incident, material audit, strategy
   policy change, or deliberate deferment.

## Memory Update Contract

Keep this document compact enough to print its opening brief, but detailed
enough that another engineer can understand the last failure without chat
history. When updating:

1. Timestamp the last production verification.
2. Replace stale status claims rather than stacking contradictory ones.
3. Record the root cause, not only symptoms.
4. Record exact merged/deployed revisions and objective verification.
5. Separate completed work, deliberate retention, and true remaining work.
6. Preserve safety and P&L interpretation rules.
7. Never add secrets, exact access identifiers, key paths, or commands
   containing those sensitive values.
