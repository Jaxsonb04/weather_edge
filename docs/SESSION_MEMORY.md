# WeatherEdge Session Memory

Last updated: 2026-07-27 03:20 PDT

Last production verification: 2026-07-27 03:15 PDT

Status: production healthy, current, and paper-only on runtime revision
`b5ae442b22d37ac6ad831db02e7c50a5309a47fc`

This is the rolling cross-session handoff for WeatherEdge. It records the last
verified state and the reasoning behind it. It is not a substitute for checking
current AWS state before making an operational claim.

## Session Brief

- **Execution-bar alignment (2026-07-27, PR #69, runtime revision
  `b5ae442b22d37ac6ad831db02e7c50a5309a47fc`):** the 07-26 capture release was
  a near-no-op in production. On 07-26/27 the live book recorded 23 approved
  candidates and placed ZERO orders: every one had a one-tick spread and an
  after-fee lower-bound edge of 0.002-0.007, while the execution layer still
  demanded the 0.02 MAKER reservation margin. Two fixes: a natural cross
  (bid+1 already at the ask) now routes through the taker path instead of
  being judged against a margin that exists to cover adverse selection on a
  RESTING quote, and the live crossing bar is now the approval gate's own
  floor (non-negative after-fee edge against the modelled lower bound).
  Measured on settled outcomes with the repo's canonical
  `settlement_truth` rule -- which reproduces the engine's realized P&L on
  283/283 settled orders with zero mismatches -- the change moves live from
  58 positions / 87.9% win / $1.33 per day (day-clustered 95% CI
  -$0.41..+$3.14) to 144 positions / 92.4% win / $3.22 per day (CI
  +$0.27..+$5.94). No decision or safety gate moved.

- **MEASURED CEILING -- read before promising a daily number.** A 36-agent
  adversarially-verified analysis over the full decision journal established
  that the binding constraint is DISPLAYED LIQUIDITY, not the model, the
  gates, sizing, or exits. Median `recommended_contracts` is 88 against a
  median displayed ask of 5, and **97.4% of live approved candidates are
  depth-bound**. At recommended Kelly size the approved population was worth
  ~$41/day; capped at the depth actually shown it is ~$5/day. Peak daily
  capital deployed was $76 of a $180 budget (42%) -- the book is
  liquidity-starved, not capital- or gate-starved. Best case with three
  entries per market/side and perfect capture is $9.29/day; a realistic
  post-change run rate is **$4-6/day, with $10 days on roughly the 40% of
  days when depth is generous**. **$10/day is not reachable as a sustained
  average on this liquidity, and Research ROI's $50/day is roughly 3x above
  its measured ceiling (~$18.60/day even granted live-like caps).** Closing
  the gap needs more markets or deeper books, not looser gates.

- **What was measured and REJECTED** (do not re-propose without new
  evidence): loosening any rejection bucket -- every one loses money on
  settled outcomes (sleeve edge/LCB -$0.028/-$0.032 per contract, source
  spread -$0.047 and monotonically worse with spread, the 1c/2c tail rule
  0/34 wins, model/market gap -$0.024); the live `edge_lcb >= 0` floor's
  marginal population is a null (n=28, -$0.0019/contract, t=-0.03); the
  same-day `min_lead_days=1` block (research same-day is ~6x worse than
  next-day, and all 7 signal-approved blocked live candidates had zero
  displayed depth, so $0.00 was forgone); per-city selection (permutation
  test p=0.538 live, p=0.958 research); narrowing or widening the 0.70-0.97
  favorite band; banking profits earlier (every variant loses at every
  level); raising position caps (buys ~$1/day while the worst position grows
  to 201% of the daily-loss breaker, and inflates the bucket with the LOWEST
  settled ROI); and a research policy v4 with live-like caps (order coverage
  of approved candidates is already 100%, so it cannot add a filled
  contract).

- **Position accumulation was measured, not assumed.** Allowing 2/3/5 lots
  per market-side lifts live to $3.55/$5.27/$6.67 per day but pushes the
  day-clustered CI lower bound to -$1.00/-$0.20/+$1.09 and the worst day
  from -$13.30 to -$20.09 (the 2% daily-loss breaker), while capital
  efficiency falls. It also requires relaxing the side-agnostic
  `has_active_paper_entry` guard, which additionally prevents holding YES and
  NO on the same market. Deferred as the highest-EV candidate for a future
  walk-forward, not shipped.

- **Highest-value next step is instrumentation, not tuning.** Only
  top-of-book depth is persisted, so whether walking one or two ticks deeper
  into the ladder would lift the ceiling is currently UNANSWERABLE. Record
  the top ~3 ladder levels per side plus a per-attempt fill record (quoted
  price, mode, filled/expired, depth visible at attempt), mindful that
  `decision_snapshots` is already the table under retention pressure. Then
  hold live behavior steady for a ~30-day window: the capture release has
  only one day of evidence (07-26 filled 3 of 7 placements, 43%, against a
  15.6% baseline of 26/167 over 07-18..07-25).

- **A methodology warning that invalidated an early pass.** `decision_snapshots`
  recording changed TWICE inside retention: research began recording the FULL
  ladder on 07-19 (~60k rows/day) and live on 07-24 (43 -> 64,872 rows). Any
  approval-RATE comparison across that boundary is a denominator artifact.
  Use `approved`/`signal_approved` counts and de-duplicate to DISTINCT
  (target_date, market_ticker, side). Live distinct approved opportunities
  per day: 20, 18, 15, 21, 8, 4, 8, 4, 8, 3 (07-18..07-27).

- **Execution-capture release (2026-07-26 evening):** PRs #66 and #67 are
  merged and deployed at runtime revision
  `5f5dc1e05e0a40524042710c9943f1290a02d2be`. July's tightening favorite
  books (displayed ask depth 21 -> 4-6 contracts, spreads 3.6c -> 1.5-1.9c)
  had starved maker-only entries: live maker quotes filled under 20% (46/49
  expired 07-18; 0/3 on 07-22) and live realized P&L fell from ~$10/day to
  ~$0 while approved candidates carried positive after-fee taker-cost edge.
  The release changes EXECUTION only: (1) live taker-cross when the
  after-fee LOWER-BOUND edge at the displayed ask clears the SAME 2% buffer
  the maker path enforces (whole contracts, depth-capped, >= $5 crossing
  notional); (2) live reservation-price resting fallback (rest at the
  highest tick preserving the buffer instead of dropping the candidate);
  (3) target-research crossing at its UNCHANGED zero after-fee point/LCB
  floor, only when displayed depth absorbs the entire intended size, so the
  v3 allocator's policy-sized resting path is preserved. No decision gate,
  sizing cap, account policy, loss pause, or readiness scope changed; the
  frozen `StrategyConfig()` defaults keep every new flag off. Live strategy
  fingerprints moved with the config (limit `b0075c015530e830c11c588b`,
  market `b0fece729659b86d2e1e35f1`); readiness treats any non-legacy
  fingerprint as valid, so the promotion clock is unaffected.
- **Post-deploy state:** cutover validation reported exactly two
  fixed-capital ledgers; build_info matched the new revision with a clean
  tree; 0 failed units; 12/12 timers enabled and active; scheduler health
  succeeded; the first post-deploy scans gated normally and the first new
  order was a policy-sized resting research entry (the preserved allocator
  path). No capture-eligible live candidate appeared in the overnight
  window; watch the first liquid US afternoon for taker-cross fills
  (PAPER_FILLED, entry_mode=limit, filled_at=created_at, price at the ask).
- **Production release:** PR #58 is merged. The EC2 runtime, generated
  artifacts, public manifest, and rebuilt React app shell were verified against
  runtime revision `71ac845422fc75cc35e24bb3b3a918dd44f917b3`. The app-shell
  checksum matched locally, on EC2, and on the public site.
- **Fresh account era:** exactly two `$1,000` paper ledgers are active. **Live
  Stability** prioritizes win rate, consistency, and controlled growth and is
  the only readiness profile. **Research ROI** accepts higher bounded paper risk
  against a fixed `$50/day` KPI, equal to 5% of original capital, and is
  excluded from live readiness.
- **Preserved history:** five prior accounts are archived and entry-frozen.
  Sixty-two legacy open paper positions remain settlement-active in the normal
  monitor/settlement lifecycle; no orders, fills, P&L, or resting history were
  deleted or reassigned to either fresh ledger.
- **Accounting safety:** both active ledgers were `$1,000`, `ACTIVE`, and
  reconciled with zero open or pending positions at verification. Invalid
  account identity or reconciliation now fails closed: active profile,
  accounting, and readiness displays disappear instead of inferring a balance.
- **Readiness:** Live Stability is the sole readiness profile, using valid live
  evidence across the legacy and fresh live identities. The deployed status
  was `REPLAY_REQUIRED` with 5 of 12 checks passed; every research account was
  explicitly excluded. This is not real-money ready.
- **Reliability:** Strategy Lab now uses a persistent fixed five-minute
  wall-clock timer. An offset scheduler watchdog verifies the 11 application
  timers, all canonical unit definitions, database/disk health, hashes, source
  provenance, and local/public freshness. It can repair only bounded
  publication staleness and never starts scan, monitor, settlement, or another
  trading action. A separate production canary verifies all 12 timers,
  including the watchdog.
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
  source-matched, and snapshot-identical. Natural Strategy cycles generated at
  10:40 and 10:45 UTC after deployment; the latest was present in the public
  manifest published at 10:50 UTC.
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
- entry-frozen archived histories with settlement state shown explicitly.

Missing historical account balance is now shown as unavailable rather than
synthesized from attributed P&L.

### Network location changed

The owner's public IP changed between houses, so the narrow SSH allowlist no
longer matched. This interrupted operator access but did not prove an
application failure. A narrow owner rule restored access and is intentionally
retained for the next session.

### Maker-only entries starved once the market tightened

After 07-22 the favorite books thinned (displayed ask 21 -> 4-6 contracts,
spreads 3.6c -> 1.5-1.9c) and live volume collapsed (45 placements/day ->
2-9) while win rate stayed ~100%: a fill-capture failure, not an edge
failure. Journal counterfactuals showed capture-eligible expected profit of
$0.8-2.2/day (live, at the unchanged 2% LCB bar with >= $5 depth) and
$8.6-44.5/day (research target, at its unchanged zero floor with full depth
coverage). The 07-26 evening release converts exactly that eligible set to
immediate fills and rests everything else as before. It cannot conjure edge
on days the bars fail; expect live recovery toward $10/day only when
capture-eligible candidates exist, and treat the research $50/day KPI as an
aspiration the market may simply not offer on quiet days.

### Deploy preflight ran out of disk twice, then needed a modern python3

Back-to-back deploys hit the backup preflight space check (snapshot +
restore copy + 1 GiB ~= 19.6 GB): KEEP_DAYS=1 never removes same-day
snapshots, so each earlier same-day deploy's verified 9 GB local rollback
snapshot had to be removed manually after confirming its off-host copy and
checksum existed. Separately, sync_to_box.sh shells `python3` for the
execution/accounting version stamps; the Mac's default python3 is Xcode
3.9 (no `datetime.UTC`) and one run aborted after quiesce+transfer,
leaving the box safely quiesced with the maintenance marker as designed.
The rerun used a PATH with a modern python3 first. Rerun hazard to know:
capture records the CURRENTLY-enabled timer policy, so a rerun from the
quiesced state would capture an empty policy and restore nothing;
neutralized by `systemctl enable` (without `--now`) of the 12-timer policy
before rerunning, so capture saw the true policy while nothing ran
unvalidated.

### Supply decline is a market condition, not a fixable data bug

The LAMP/GFS-MOS HTTP 403s look alarming but do NOT explain the decline: those
feeds supply station-guidance features, not the NWP ensemble that drives EMOS.
`nwp_model_forecasts` is stable at 8 models x 240 rows/day through 07-27
(only `gfs_graphcast025` stopped, on 2026-05-21, two months before the decay).
What actually happened is that the crowd priced closer to the model: mean edge
on APPROVED live rows fell 0.064 -> 0.033-0.056, spreads tightened 3.6c ->
1.5-1.9c, and displayed depth fell 21 -> 4-11 contracts. Distinct approved
opportunities fell from ~20/day to 3-8/day. Nothing in our control caused it
and no safe parameter change reverses it.

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
- Archived every prior or unknown account identity and rejected new-entry
  admission to archived or ambiguous identities.

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
- Natural five-minute Strategy cycles generated successfully at 10:40 and
  10:45 UTC with schema v3, current accounting, and live-only readiness. The
  latest was promoted in the public manifest at 10:50 UTC.

## Paper Performance Snapshot

These are paper results and research evidence, not promised returns. The table
stops at the last completed published day, 2026-07-25.

| Date | Legacy live strategy attribution | Cross-account Strategy Lab total |
| --- | ---: | ---: |
| 2026-07-20 | +$9.9765 | +$17.4301 |
| 2026-07-21 | +$9.91 | +$18.89 |
| 2026-07-22 | +$7.57 | +$18.22 |
| 2026-07-23 | +$0.83 | +$11.58 |
| 2026-07-24 | +$0.20 | +$7.95 |
| 2026-07-25 | +$0.00 | +$4.38 |

The cross-account column combines strategy-attributed outcomes from
economically separate historical paper accounts and must never be described as
one bankroll's return.

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
5. Continue observing later natural Strategy refreshes and archived-position
   settlements.
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
