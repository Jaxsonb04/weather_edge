# WeatherEdge Strategy-Era Restart — Fable Review Handoff

> Paste this into a fresh Fable session at high or xhigh effort. This is an
> independent, read-only release review. Do not implement fixes in the review
> session.

## Mission

Independently verify WeatherEdge's strategy-era restart at runtime revision
`71ac845422fc75cc35e24bb3b3a918dd44f917b3`.

Determine whether the two-profile account restart, preserved history,
fail-closed accounting/readiness, scheduler reliability, public Strategy Lab,
security hardening, and paper-only safety controls are correct and genuinely
live. The site is recruiter-facing, so it must never conflate economically
separate accounts, invent account balances, overstate returns, or silently fall
behind.

Lead your final response with `PASS` or `BLOCKED`.

## Current State

- PR #58 is merged.
- The deployed runtime revision is
  `71ac845422fc75cc35e24bb3b3a918dd44f917b3`.
- The runtime/data release and rebuilt React app shell were both deployed.
- Exactly two fresh `$1,000` paper ledgers should be active:
  - **Live Stability** — conservative win-rate/stability profile and the only
    readiness profile.
  - **Research ROI** — higher bounded paper risk with a fixed `$50/day` KPI,
    excluded from live readiness.
- Five prior accounts should be archived and entry-frozen. Legacy open
  positions should remain settlement-active without allowing new entries.
- Real-money execution should be disabled and dry-run enabled.
- The narrow owner SSH rule is intentionally retained for a later session.
  Never print or alter its identifiers.

## Evidence Already Recorded

Treat this as a claim list to reproduce, not proof to trust:

- GitHub CI passed on Python 3.12, Python 3.13, and Bun.
- Full backend/forecaster suite: 2,469 passed, 8 skipped.
- Frontend suite: 153 passed.
- Deployment/watchdog suite: 146 passed.
- Independent focused review: 186 backend/security/deploy and 51 frontend
  checks passed.
- Production canary recorded zero failed units, 25 canonical units matching
  source, 12/12 timers enabled and active, disk at 68.4%, and successful
  scheduler health.
- Natural five-minute Strategy cycles generated at 10:40 and 10:45 UTC with
  schema v3, current accounting, and live-only readiness. The latest was present
  in the public manifest published at 10:50 UTC.
- Production accounting recorded two active `$1,000`, `ACTIVE`, reconciled
  ledgers and five archived accounts retaining 62 open legacy positions.
- Both fresh ledgers recorded `$1,000` realized equity, `$0` realized P&L, and
  zero open or pending positions. Legacy live strategy attribution was
  `+$45.70`, while the old shared account's true realized P&L was `-$41.62`;
  those values are intentionally not the same economic claim.
- Research ROI's `$50` activation-day KPI was missed and infeasible from the
  current scan's conservative expected-profit evidence.
- The deployed readiness status was `REPLAY_REQUIRED`, with 5 of 12 checks
  passed and every research account excluded. It was not real-money ready.
- Public/local manifests were fresh, source-matched, and snapshot-identical.
- The React shell checksum matched local `dist`, EC2 `webdist`, and public
  `index.html`.
- Rapid-hover QA recorded one visible tooltip, one active dot, zero cursor
  remnants, and a current-day third row of `Account balance $1,000.00`.
- A 390 px mobile pass recorded no page-level horizontal overflow.

## Decisions To Preserve

- Live Stability is the sole readiness profile and prioritizes dependable wins,
  controlled drawdown, and steady growth. Its readiness computation may use
  valid live evidence across legacy and fresh live account identities.
- Research ROI maximizes bounded paper ROI against the fixed 5% of original
  capital KPI; it never contributes to live readiness.
- Historical books accept no new entries. Existing legacy positions remain
  settlement-active and may update achieved performance as they settle.
- Strategy attribution and economic account balance are separate concepts.
- Cross-account totals are never one bankroll.
- A missing historical balance remains unavailable; it is never reconstructed
  from attributed P&L.
- Real-money execution stays disabled.
- `$10/day`, `$16/day`, and 5% per day are targets or historical observations,
  not guaranteed returns.
- Safety, after-fee edge, liquidity, loss-pause, exposure, calibration, and
  evidence gates may not be weakened merely to create activity.
- Do not remove the narrow owner SSH rule or expose operator identifiers.

## Review Work

1. Read `AGENTS.md`, `CLAUDE.md`, and `docs/SESSION_MEMORY.md` in full before
   making a claim.
2. Inspect PR #58 and the complete diff from its merge base.
3. Verify local and GitHub state. Distinguish later documentation-only commits
   and current documentation HEAD from the deployed runtime code revision.
4. Verify deployed build provenance and public artifact provenance against
   `71ac845422fc75cc35e24bb3b3a918dd44f917b3`.
5. Confirm exactly two active fixed-capital ledgers, canonical account/profile
   identity, archived-account new-entry rejection, account-scoped balances, and
   order/ledger lifecycle reconciliation. Reproduce the fresh-ledger `$1,000 /
   $0 / 0 open / 0 pending` state, the `+$45.70` legacy-live strategy
   attribution versus `-$41.62` shared-account P&L distinction, and the missed,
   currently infeasible `$50` Research ROI KPI.
6. Confirm malformed or unavailable active accounting suppresses active
   profiles and readiness in both backend publication and frontend parsing.
7. Confirm readiness uses valid live evidence across legacy and fresh live
   account identities, remains scoped to the Live Stability profile, and
   excludes every research account, including the active Research ROI ledger.
8. Confirm Strategy uses a persistent wall-clock five-minute schedule and that
   the offset watchdog checks the 11 application timers, all canonical unit
   definitions, database/disk health, hashes, provenance, and freshness.
   Confirm the separate production canary checks all 12 timers, including the
   watchdog.
9. Prove the watchdog's repair allowlist cannot start scan, monitor,
   settlement, or another trading action.
10. Review the deployment maintenance marker and exit/signal recovery path.
    A failed post-cutover deploy must either restore the captured timer policy
    and release maintenance or re-quiesce safely.
11. Recheck the root/app lock boundary and symlink non-truncation regression.
12. Recheck NWS scheme, exact host, credentials, port, redirect, and response
    size controls.
13. Reproduce relevant test, lint, build, compilation, shell, dependency, and
    static-analysis gates.
14. Use current read-only AWS evidence to inspect failed units, all 12 timers,
    canonical unit integrity, scheduler result, disk, manifest freshness,
    active-ledger reconciliation, readiness scope, and paper-only flags.
15. Drive the deployed Strategy Lab on desktop and a real mobile viewport:
    - verify both fresh profiles;
    - verify Achieved performance & profiles;
    - verify archived open positions are clearly settling;
    - inspect Positions & execution log spacing and the compact pending-limit
      empty state;
    - rapidly move across equity-chart dates;
    - require one visible tooltip, no ghost cursor/border, Daily P&L, cumulative
      P&L, and true Account balance when that day's backend balance exists.
16. Confirm public shell, data, and manifest are mutually current rather than a
    new JSON schema behind an old app shell.

## Constraints

- Review only. Do not edit code or documentation.
- Do not mutate AWS, start or stop services, invoke scheduler repair, trigger a
  trade, rerun deployment, delete resources, clean files, change access, or
  merge/push anything.
- Use current-run tool evidence for every completion claim.
- Do not diagnose production from ignored local runtime databases or JSON.
- Do not reveal network addresses, key paths, cloud account identifiers,
  security-group identifiers, backup locations, credentials, or secrets.
- If AWS access is unavailable, continue local and public review and mark AWS
  claims `UNKNOWN`; do not silently assume they passed.
- Do not re-litigate the user's two-profile product decision.

## Artifacts To Inspect

- `docs/SESSION_MEMORY.md`
- `trading/sfo_kalshi_quant/account.py`
- `trading/sfo_kalshi_quant/research_policy.py`
- `trading/sfo_kalshi_quant/profile_identity.py`
- `trading/sfo_kalshi_quant/db.py`
- `trading/sfo_kalshi_quant/replay.py`
- `trading/sfo_kalshi_quant/strategy_lab/build.py`
- `trading/deploy/aws/sync_to_box.sh`
- `trading/deploy/aws/validate_account_cutover.py`
- `trading/deploy/aws/check_scheduler_health.sh`
- `trading/deploy/aws/disable_systemd_timers.sh`
- `trading/deploy/aws/systemd/sfo-strategy-lab-refresh.timer`
- `trading/deploy/aws/systemd/sfo-scheduler-health.timer`
- `forecaster/blend_sources.py`
- `src/lib/strategy.ts`
- `src/components/strategy/EquityCurve.tsx`
- `src/components/strategy/OpenBook.tsx`
- `src/components/strategy/ArchivedPerformance.tsx`
- `src/components/views/StrategyLabView.tsx`
- The tests introduced or changed by PR #58.

## Open Questions

- Did any post-release timer, artifact, or shell drift occur after the recorded
  canary?
- Do subsequent natural Strategy refreshes remain on the five-minute wall
  clock?
- Do the 62 archived positions continue settlement without admitting a new
  entry order for an archived account?
- Do the previously observed LAMP/GFS-MOS archive HTTP 403 responses persist?

Treat these as observations to verify where current evidence is available, not
as automatic release blockers.

## Process State

- Runtime/data deploy: complete.
- Historical analysis cache refresh: complete.
- React app-shell deploy: complete.
- Public propagation: complete at last verification.
- Independent production canary: passed with zero failures.
- Local preview and ignored runtime state are not production authority.

## Exact Next Action

Start by printing the current `Session Brief` from `docs/SESSION_MEMORY.md`.
Then establish the checked-out commit, inspect PR #58, and reproduce the
highest-risk invariants before doing broad review:

1. exactly two active reconciled `$1,000` ledgers;
2. live-only readiness;
3. real-money disabled/dry-run enabled;
4. watchdog unable to start trading actions; and
5. public source/shell/data parity.

## Receiver Directive

Return:

1. `PASS` or `BLOCKED`;
2. release-blocking findings first, with severity and precise evidence;
3. verified controls;
4. unknowns or observations still requiring time;
5. the exact commands/tests/browser interactions used; and
6. an explicit statement when no release blocker remains.

Do not provide a speculative refactor list. If you find a real defect, explain
its input/state, resulting failure, affected file/component, blast radius, and
the smallest safe remediation, but do not implement it in this review session.
