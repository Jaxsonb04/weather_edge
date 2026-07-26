# WeatherEdge Session Memory

Last updated: 2026-07-26 03:40 PDT

Last production verification: 2026-07-26 03:38 PDT

Status: production healthy, current, and paper-only on runtime revision
`71ac845422fc75cc35e24bb3b3a918dd44f917b3`

This is the rolling cross-session handoff for WeatherEdge. It records the last
verified state and the reasoning behind it. It is not a substitute for checking
current AWS state before making an operational claim.

## Session Brief

- **Production release:** PR #58 is merged. The EC2 runtime, generated
  artifacts, public manifest, and rebuilt React app shell were verified against
  runtime revision `71ac845422fc75cc35e24bb3b3a918dd44f917b3`. The app-shell
  checksum matched locally, on EC2, and on the public site.
- **Fresh account era:** exactly two `$1,000` paper ledgers are active. **Live
  Stability** prioritizes win rate, consistency, and controlled growth and is
  the only readiness profile. **Research ROI** accepts higher bounded paper risk
  against a fixed `$50/day` KPI, equal to 5% of original capital, and is
  excluded from live readiness.
- **Preserved history:** five prior accounts are archived read-only. Sixty-two
  legacy open paper positions remain in normal monitor/settlement lifecycle;
  no orders, fills, P&L, or resting history were deleted or reassigned to either
  fresh ledger.
- **Accounting safety:** both active ledgers were `$1,000`, `ACTIVE`, and
  reconciled with zero open or pending positions at verification. Invalid
  account identity or reconciliation now fails closed: active profile,
  accounting, and readiness displays disappear instead of inferring a balance.
- **Readiness:** only canonical Live Stability evidence is eligible. The
  deployed verdict was `REPLAY_REQUIRED` with 5 of 12 checks passed; research
  accounts were explicitly excluded. This is not real-money ready.
- **Reliability:** Strategy Lab now uses a persistent fixed five-minute
  wall-clock timer. An offset scheduler watchdog verifies all canonical timers,
  effective units, database/disk health, hashes, source provenance, and
  local/public freshness. It can repair only bounded publication staleness and
  never starts scan, monitor, settlement, or another trading action.
- **Public experience:** the Strategy Lab now exposes both fresh profiles,
  achieved-performance history, daily and cumulative P&L, and true account
  balance. Rapid-hover QA left one tooltip, one active dot, and zero ghost
  cursors; the current-day tooltip showed `$1,000.00` account balance. Desktop
  and 390 px mobile layouts had no page-level horizontal overflow.
- **Execution layout:** Positions & execution log now has balanced gutters,
  more separation from dividers and P&L, and a compact pending-limit empty
  state. The public desktop and mobile layouts were inspected after deployment.
- **Operational health:** zero failed units; 25 canonical systemd units matched
  source; 12 of 12 timers were enabled and active; scheduler health succeeded;
  disk use was 68.4%, below the 85% guard; public and local manifests were fresh,
  source-matched, and snapshot-identical.
- **Safety and access:** real-money execution remains disabled and dry-run
  remains enabled. Keep the narrowly scoped owner SSH rule for the next session;
  revoke it only when the owner says access is no longer needed. Its identifiers
  remain only in ignored operator state.

## What Went Wrong

### Strategy Lab appeared behind

There were two different causes across the recovery:

1. The earlier production Strategy tail query used parameterized SQLite limits.
   Production SQLite selected a full scan over a large decision journal, causing
   a service timeout and stale public Strategy data. PRs #55 and #56 restored a
   bounded indexed plan and separated overlapping maintenance windows.
2. During this restart, the new schema, profiles, and interface existed on the
   implementation branch before production had received both release halves.
   The public site therefore still showed the prior schema/account era until the
   runtime/data sync and separate prebuilt React app-shell sync both completed.
   This was deployment lag, not evidence that the paper engine had stopped.

The release was completed through both audited paths and then verified by exact
source, manifest, app-shell checksum, DOM, and screenshot evidence.

### Completion-relative scheduling could drift

The Strategy timer was completion-relative. A slow cycle moved the next cycle
later, and no independent scheduler knew whether publication was stale, a unit
had drifted, or trading was intentionally paused. The new calendar timer is tied
to wall-clock time. The new watchdog distinguishes safe age-only publication
repair from unsafe timer/unit state and refuses to auto-repair trading services.

### Legacy attribution was easy to mistake for account equity

Early live and research strategies shared economic paper accounts, while later
research policies used isolated accounts. A profile-attribution curve could
therefore be misread as that profile's bankroll, and cross-account totals could
be described as one balance. The new publication and UI separate:

- strategy-attributed P&L;
- true economic account balances;
- two fresh active ledgers; and
- read-only archived accounts.

Missing historical account balance is now shown as unavailable rather than
synthesized from attributed P&L.

### Network location changed

The owner's public IP changed between houses, so the narrow SSH allowlist no
longer matched. This interrupted operator access but did not prove an
application failure. A narrow owner rule restored access and is intentionally
retained for the next session.

### Profit targets were being treated too literally

The July 20 live paper result demonstrated that a strict, high-confidence,
larger-size NO position could produce roughly `$10` in one day. It did not prove
that `$10`, `$16`, or 5% can be repeated daily. Counterfactual review did not
support blindly holding 98-cent exits to settlement merely to collect the last
two cents; Live Stability keeps profit-banking and safety gates. More aggressive
exit and sizing ideas belong in Research ROI, still bounded and paper-only.

## What We Accomplished

### Two-profile strategy restart

- Created one fresh Live Stability paper ledger with `$1,000`.
- Created one fresh Research ROI paper ledger with `$1,000`.
- Fixed the Research ROI daily KPI at `$50`, measured against original capital,
  rather than allowing the denominator or target to drift.
- Kept Live Stability conservative and readiness-bearing.
- Allowed Research ROI higher bounded paper risk while excluding it from
  readiness and live goals.
- Removed the legacy motion book from recurring entry scans. Its retained
  positions still monitor and settle normally.
- Archived every prior or unknown account identity and rejected new writes to
  archived or ambiguous identities.

### Accounting, identity, and replay correctness

- Added canonical account/profile identity across admission, orders, replay,
  reports, and readiness.
- Added exact active-order/ledger lifecycle reconciliation covering fills,
  resting and partial orders, terminal rows, missing rows, and orphans.
- Made deployment cutover require exactly two active fixed-capital reconciled
  ledgers.
- Published Strategy schema v3 with exact active ledgers, archived accounts,
  pending risk/count, and backend-provided `closing_equity`.
- Restricted readiness to valid live evidence across legacy and fresh live
  identities. Research evidence cannot cross into the live cohort.
- Made backend and frontend independently fail closed if active accounting is
  unavailable or malformed.

### Strategy Lab and recruiter-facing design

- Added **Achieved performance & profiles**.
- Separated Strategy attribution from Historical account balances.
- Added true total balance to profile summaries.
- Added account balance as the third tooltip metric when the backend has
  economic balance for that day; older attribution-only days remain honest.
- Disabled tooltip animation and cursor rendering to remove rapid-hover
  remnants and clipped borders.
- Improved profile naming, readiness language, current-book spacing, and the
  pending-limit empty state with the existing HeroUI Pro design system.
- Verified the deployed page at desktop and real 390 px mobile viewports.

### Timers, deployment, and workload reduction

- Replaced Strategy's completion-relative timer with a persistent five-minute
  `OnCalendar` schedule.
- Added a five-minute offset scheduler-health timer.
- Added a root-owned deployment-maintenance marker and recovery trap that
  either restores the captured timer policy or safely re-quiesces.
- Added unit-integrity, database/disk, artifact checksum, provenance, and
  local/public freshness checks.
- Limited automatic repair to Strategy/publication age-only failures.
- Kept the app-shell and runtime publication writers quiesced during the final
  web sync, then restored both timers and reran scheduler health.
- Preserved one short-lived local rollback snapshot plus its independently
  downloaded, integrity-checked, encrypted off-host copy. Normal retention
  removes old local snapshots.

### Security and maintenance

- Restricted NWS-advertised URLs to HTTPS, the exact expected host, no
  credentials, port absent or 443, revalidated redirects, and a 4 MiB cap.
- Fixed the scheduler's root/application lock boundary so validation and lock
  acquisition run as the unprivileged app user; the symlink non-truncation
  regression passes.
- Added a seven-day Dependabot cooldown for routine churn; security updates
  continue to bypass cooldown.
- Cleared stale ignored local runtime databases/data through the canonical
  cleanup script. AWS-generated runtime state remains authoritative.
- Removed redundant recurring motion scans and retained only data/history still
  needed for settlement, research, rollback, or operation.

## Verification Evidence

- PR #58 merged at
  `71ac845422fc75cc35e24bb3b3a918dd44f917b3`.
- GitHub CI: Python 3.12, Python 3.13, and Web (Bun) all passed.
- Full backend/forecaster suite: 2,469 passed, 8 skipped.
- Frontend suite: 153 passed.
- Deployment/watchdog suite: 146 passed.
- Independent focused review: 186 backend/security/deploy checks and 51
  frontend checks passed with no release blocker.
- Production build, lint, icon integrity, Python compile, AWS shell syntax,
  YAML parsing, diff checks, and Bun dependency audit passed.
- The deployment backup was uploaded encrypted, downloaded independently,
  checksum-matched, and passed SQLite integrity and foreign-key checks before
  source transfer.
- Independent post-deploy canary: 0 failures.
- Public app-shell checksum matched local `dist/` and EC2 `webdist`.
- Rapid-hover stress: one visible tooltip, one active dot, zero tooltip cursors.

## Paper Performance Snapshot

These are paper results and research evidence, not promised returns.

| Date | Legacy live paper account | Cross-account Strategy Lab total |
| --- | ---: | ---: |
| 2026-07-20 | +$9.9765 | +$17.4301 |
| 2026-07-21 | +$9.91 | +$18.89 |
| 2026-07-22 | +$7.57 | +$18.22 |
| 2026-07-23 | +$0.83 | +$11.58 |
| 2026-07-24 | +$0.20 | +$7.95 |
| 2026-07-25 | +$0.00 | +$4.39 |

The cross-account column combines economically separate historical paper
accounts and must never be described as one bankroll's return.

At the 2026-07-26 verification:

- Fresh Live Stability: `$1,000.00` realized equity, `$0.00` realized P&L,
  zero open positions, zero pending limits.
- Fresh Research ROI: `$1,000.00` realized equity, `$0.00` realized P&L, zero
  open positions, zero pending limits.
- Archived accounts: five.
- Archived open positions: 62 total; 10 target-v1 and 52 motion positions were
  still settling.
- Legacy live strategy attribution: `+$45.70` across 80 resolved positions,
  62 wins and 18 losses.
- The legacy shared live account's true all-time realized P&L remained
  `-$41.62`; it is deliberately not presented as the legacy live strategy's
  attributed balance.
- Research ROI's `$50/day` goal was not achieved on activation day and was not
  feasible from the current scan's conservative expected-profit evidence.

## Safety And Interpretation Rules

- Never enable real-money trading as part of an audit, recovery, target chase,
  or UI change.
- Never promise `$10/day`, `$16/day`, 5% per day, or any return. Targets are
  paper research KPIs.
- Never weaken a `NO_TRADE`, after-fee edge, loss-pause, exposure,
  calibration, liquidity, or evidence gate merely to increase activity.
- Report each economic account separately from strategy attribution and
  cross-account research totals.
- Never synthesize account balance from attributed P&L when the backend did not
  publish a true balance.
- Keep AWS access identifiers, credentials, network addresses, and key paths
  out of this file and all tracked project artifacts.

## Known Nonblocking Concerns

- The watchdog deliberately refuses to auto-repair disabled or drifted trading
  timers. That requires operator investigation; only age-only Strategy and
  operational publication staleness is safely repairable.
- LAMP and GFS-MOS NOAA archive paths previously returned HTTP 403 during
  backfill. IEM, Open-Meteo previous runs, NBM, HRRR, truth, and EMOS remained
  available. Replace or remove those fetch paths if the 403 responses persist.
- The runtime revision above is the deployed code revision. A later
  documentation-only commit containing this memory and the reviewer prompt does
  not require another 9.5 GB database-backed runtime deployment because it
  changes no deployed package, unit, SPA asset, or artifact generator.

## Next Session Checklist

1. Print the `Session Brief` above before taking action.
2. Confirm the checkout is clean and compare local `HEAD` with `origin/main`.
3. When production state matters, freshly revalidate runtime source, failed
   units, all 12 timers, unit integrity, manifest parity/freshness, disk, active
   ledger reconciliation, readiness scope, and real-money safety flags.
4. Keep the narrow owner SSH rule until the owner says access is finished.
5. Observe the next natural Strategy refresh and archived-position settlements.
6. Treat ignored local runtime files as disposable; use AWS/public artifacts.
7. Update this file after every material incident, deployment, policy change,
   deliberate deferment, or production verification.

## Memory Update Contract

Keep this document compact enough to print its opening brief, but detailed
enough that another engineer can understand the last failure without chat
history. When updating:

1. Timestamp the last production verification.
2. Replace stale status claims rather than stacking contradictions.
3. Record root cause, not only symptoms.
4. Record exact merged/deployed revisions and objective verification.
5. Separate completed work, deliberate retention, and true remaining work.
6. Preserve safety and P&L interpretation rules.
7. Never add secrets, exact access identifiers, key paths, or sensitive
   operator commands.
